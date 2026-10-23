"""Short event batches and an idempotent 20:00 digest. No model controls sending."""
import argparse
import datetime as dt
import fcntl
import json
import os
import re
from pathlib import Path
from zoneinfo import ZoneInfo

import notify
import price_signals
from filters import gpu_models, specifications, MIN_PRICE
from ranking import eligible, rank, market_top, manual_items, recent_card
from tracking import timestamp
from preferences import hidden

ROOT = Path(__file__).resolve().parent
STATE = ROOT / 'results/monitor'
MOSCOW = ZoneInfo('Europe/Moscow')


def read(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def save(path, value):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    tmp.replace(path)


def hardware(item):
    specs = specifications({**(item.get('card') or {}), 'id': item['id']})
    gpu = specs['gpu'] + (f" {specs['vram_gb']} GB" if specs.get('vram_gb') else ' — видеопамять неизвестна')
    return f"{gpu}\n{specs['cpu']} · RAM {specs['ram']} · {specs['storage']}"


def compact(item, prefix, include_url=True):
    info = rank(item)
    seller = item['flags'].get('seller', {}).get('label', 'продавец не определён')
    caveat = f"Учесть: {info['caveat']}.\n" if info['caveat'] else ''
    comparison = notify.price_comparison(item['analysis']) + '\n' if item.get('analysis') else ''
    signal = price_signals.message(item)
    return ((signal + '\n' if signal else '')
            + f"{prefix}: {str(item['title'])[:100]} — {item['price']:g} ₽\n"
            f"{hardware(item)}\n{seller}. {info['reason']}.\n"
            f"{comparison}{caveat}" + (item['url'] if include_url else ''))


def pack(blocks, limit=3500):
    parts, current = [], ''
    for block in blocks:
        if current and len(current) + len(block) + 2 > limit:
            parts.append(current); current = ''
        current += ('\n\n' if current else '') + block
    if current:
        parts.append(current)
    return parts


def pack_digest(blocks, items, limit=3500):
    """Keep item boundaries so queued parts can honor later hide actions."""
    parts, groups, current, group = [], [], '', []
    for block in blocks:
        if current and len(current) + len(block) + 2 > limit:
            parts.append(current); groups.append(group)
            current, group = '', []
        match = re.match(r'^(\d+)\. ', block)
        number = int(match[1]) if match else 0
        item = items[number - 1] if 1 <= number <= len(items) else None
        if item and not block.startswith(f'{number}. {digest_card(item)}'):
            item = None
        group.append({'text': block, 'id': item['id'] if item else None,
                      'price': item['card']['reviewed_price'] if item else None,
                      'number': number if item else None})
        current += ('\n\n' if current else '') + block
    if current:
        parts.append(current); groups.append(group)
    return parts, groups


def digest_keyboard(blocks):
    rows = []
    for block in blocks:
        if block['id'] is not None:
            rows.append([{'text': f"🙈 Убрать №{block['number']}",
                          'callback_data': f"hide:{block['id']}:{block['price']:g}"}])
    return {'inline_keyboard': rows} if rows else None


def verified_price_history(path, ids):
    """Read only prices from cards checked in that snapshot, not search prices."""
    history = {str(identifier): {} for identifier in ids}
    if not path.exists():
        return history
    with path.open() as source:
        for line in source:
            try:
                report = json.loads(line)
            except json.JSONDecodeError:
                continue  # An interrupted final append must not block the digest.
            at = report.get('snapshot')
            for item in report.get('candidates', []):
                identifier = str(item.get('id'))
                if identifier not in history:
                    continue
                card = item.get('card') or {}
                price = card.get('reviewed_price')
                if (card.get('parsed') and card.get('price_source') == 'card'
                        and card.get('checked_at') == at and type(price) in (int, float)
                        and price > 0 and price == item.get('price')):
                    history[identifier][at] = price
    return {key: sorted(values.items(), key=lambda pair: timestamp(pair[0]))
            for key, values in history.items()}


def price_history_line(item, observations):
    if len(observations) < 2 or observations[-1][1] != item['price']:
        return ''
    first_at, first_price = observations[0]
    prices = [price for _, price in observations]
    if len(set(prices)) == 1:
        return ''
    first_date = timestamp(first_at).astimezone(MOSCOW).strftime('%d.%m')
    current = item['price']
    change = 100 * (current - first_price) / first_price
    percent = f"{change:+.1f}".replace('.', ',')
    rubles = lambda value: f"{value:,.0f}".replace(',', ' ')
    return (f"История с {first_date}: {rubles(first_price)} → {rubles(current)} ₽ "
            f"({percent}%); диапазон {rubles(min(prices))}–{rubles(max(prices))} ₽.")


def digest_card(item):
    specs = specifications({**(item.get('card') or {}), 'id': item['id']})
    models = gpu_models(specs['gpu'])
    gpu = models[0] if len(models) == 1 else specs['gpu']
    if specs.get('vram_gb'):
        gpu += f" {specs['vram_gb']} GB"
    seller_type = item.get('flags', {}).get('seller', {}).get('type')
    seller = {'private': 'Частное лицо', 'shop': 'Магазин'}.get(seller_type, 'Неизвестно')
    rubles = f"{item['price']:,.0f}".replace(',', ' ')
    lines = [f"{item['title'][:100]} — {rubles} ₽", f"Видеокарта: {gpu}",
             f"Процессор: {specs['cpu']}", f"Оперативка: {specs['ram']}",
             f"Память: {specs['storage']}", f"Продавец: {seller}"]
    signal = price_signals.message(item) if not item.get('needs_recheck') else ''
    if signal:
        lines.append(signal)
    comparison = (item.get('analysis') or {}).get('new_comparison') or {}
    if comparison.get('available'):
        saving = comparison['saving_pct']
        label = 'дешевле' if saving >= 0 else 'дороже'
        percent = f"{abs(saving):g}".replace('.', ',')
        scope = 'совпадение GPU, класса процессора и ОЗУ' if comparison['match'] == 'segment' else 'совпадение только GPU'
        date = comparison['source_date'][8:10] + '.' + comparison['source_date'][5:7]
        caveats = []
        if comparison.get('stale'):
            caveats.append('таблица старше 45 дней')
        if comparison['row'].get('issues') or comparison['row'].get('rare'):
            caveats.append('ориентир ограничен')
        detail = '; '.join([scope] + caveats)
        lines.append(f"По таблице новых от {date}: {label} на {percent}% ({detail}).")
    else:
        lines.append('По таблице новых: подходящего ориентира нет.')
    return '\n'.join(lines)


def similar_used(item, state, now, limit=3):
    models = gpu_models(specifications({**(item.get('card') or {}), 'id': item['id']})['gpu'])
    if len(models) != 1:
        return ''
    matches = []
    for peer in state.get('seen', {}).values():
        flags = peer.get('flags') or {}
        if (str(peer['id']) == str(item['id']) or hidden(state, peer['id'])
                or peer.get('availability') != 'active'
                or not flags.get('kazan_url') or flags.get('excluded_title')
                or flags.get('bulk_offer') or flags.get('condition') == 'new'
                or flags.get('rejection_reasons') or not recent_card(peer, now)):
            continue
        peer_models = gpu_models(specifications({**(peer.get('card') or {}), 'id': peer['id']})['gpu'])
        if peer_models == models:
            matches.append(peer)
    if not matches:
        return ''
    matches.sort(key=lambda peer: (peer['card']['reviewed_price'], str(peer['id'])))
    lines = ['Похожие б/у с той же видеокартой (последние проверенные цены):']
    for peer in matches[:limit]:
        price = f"{peer['card']['reviewed_price']:,.0f}".replace(',', ' ')
        lines.append(f"{peer['title'][:100]} — {price} ₽\n{peer['url']}")
    if len(matches) > limit:
        lines.append(f"Ещё {len(matches) - limit} объявлений с той же видеокартой.")
    return '\n'.join(lines)


def deliver_events(state, folder, now, sender=None):
    sender = sender or notify.send
    values = notify.config()
    if not notify.ready(values):
        return {'status': 'not_configured', 'sent': 0}
    receipt_path = folder / 'event_receipts.json'
    receipt = read(receipt_path, {})
    legacy = read(folder / 'telegram_sent.json', {})
    shown = set(legacy) | set(read(folder / 'shown_ids.json', []))
    events = list(state.get('pending_events', []))
    for item in state['seen'].values():
        if eligible(item, now) and item['id'] not in shown:
            events.append({'event_id': 'discovered:' + item['id'], 'id': item['id'], 'type': 'new', 'at': now})
    selected, ids_in_batch = [], set()
    # Prefer latest event for a given item; old changes must not describe the current price.
    for event in sorted(events, key=lambda e: e['at'], reverse=True):
        if event['event_id'] in receipt or event['id'] in ids_in_batch:
            continue
        if hidden(state, event['id']):
            continue
        item = state['seen'].get(event['id'])
        if not item or item['price'] < MIN_PRICE:
            continue
        kind = event['type']
        if kind in ('sold', 'removed', 'unavailable'):
            if item.get('availability') != kind or item['id'] not in shown:
                continue
            label = {'sold': 'Помечен как проданный', 'removed': 'Снят с публикации',
                     'unavailable': 'Объявление недоступно после двух проверок; продажа не подтверждена'}[kind]
            block = f"{label}: {item['title'][:100]}\nПоследняя цена: {item['price']:g} ₽\n{item['url']}"
        else:
            if not eligible(item, now):
                continue
            if event.get('price') is not None and event['price'] != item['price']:
                continue
            if kind == 'new' and item['id'] in shown:
                continue
            if kind.startswith('price_'):
                old = event['previous_price']; diff = item['price'] - old
                label = '📉 Цена снизилась' if diff < 0 else '📈 Цена выросла'
                block = (f"{label}: {item['title'][:100]}\n{old:g} → {item['price']:g} ₽ "
                         f"({diff:+g} ₽ / {100 * diff / old:+.1f}%)\n{hardware(item)}\n"
                         + (notify.price_comparison(item['analysis']) + '\n' if item.get('analysis') else '')
                         + item['url'])
                signal = price_signals.message(item)
                if signal:
                    block = signal + '\n' + block
            else:
                block = compact(item, '↩ Снова доступен' if kind == 'returned' else '🆕 Впервые проверен')
        selected.append((event, block)); ids_in_batch.add(item['id'])
    # Bounded batches, unsent events remain in state for later attempts.
    selected.sort(key=lambda pair: 0 if price_signals.strong_price(
        state['seen'].get(pair[0]['id'], {})) and pair[0]['type'] not in ('sold', 'removed', 'unavailable') else 1)
    selected = selected[:30]
    batches, batch, size = [], [], 0
    for pair in selected:
        if batch and size + len(pair[1]) + 2 > 3500:
            batches.append(batch); batch = []; size = 0
        batch.append(pair); size += len(pair[1]) + 2
    if batch:
        batches.append(batch)
    count = 0
    for batch in batches:
        try:
            sender('\n\n'.join(text for _, text in batch), values)
        except Exception as exc:
            return {'status': 'error', 'error_type': type(exc).__name__, 'sent': count}
        for event, _ in batch:
            receipt[event['event_id']] = now
            shown.add(event['id'])
        save(receipt_path, receipt); save(folder / 'shown_ids.json', sorted(shown)); count += 1
    return {'status': 'ok', 'sent': count, 'events': len(selected)}


def digest_blocks(state, now, pause=None, history_path=None):
    date = timestamp(now).astimezone(MOSCOW).date().isoformat()
    items = market_top(state, now)
    history = verified_price_history(history_path or STATE / 'history.jsonl', [x['id'] for x in items])
    fresh = [x for x in items if not x.get('needs_recheck')]
    stale = [x for x in items if x.get('needs_recheck')]
    local = timestamp(now).astimezone(MOSCOW)
    blocks = [f"Топ из всей базы на {local:%d.%m.%Y %H:%M} МСК — {len(items)} из максимум 10\n"
              f'Свежая проверка: {len(fresh)}; нужна перепроверка: {len(stale)}.\n'
              'Лучшие известные варианты независимо от даты публикации или обнаружения.\n'
              'Единый рейтинг по цене, конфигурации и рискам.\n'
              'Процент к новым — ориентир по вашей таблице; похожие б/у подобраны только по модели видеокарты.']
    if items:
        leader = items[0]
        cheapest = min(items, key=lambda item: (item['price'], item['id']))
        rubles = lambda value: f"{value:,.0f}".replace(',', ' ')
        comparison = (f"По вашим приоритетам №1: {leader['title'][:45]} "
                      f"за {rubles(leader['price'])} ₽.")
        if cheapest['id'] != leader['id']:
            comparison += (f" Минимальная цена: {cheapest['title'][:45]} "
                           f"за {rubles(cheapest['price'])} ₽.")
        blocks.append(comparison)
    if stale:
        blocks.append('🕒 У помеченных вариантов текущая цена и наличие не подтверждены; '
                      'указана последняя проверенная цена.')
    if pause:
        from recovery import next_retry_slot
        after = next_retry_slot(pause)
        blocks.append('⚠️ Сбор на паузе: ' + pause.get('reason', 'ошибка доступа') + '.\n'
                      f'Следующая контрольная попытка: {after:%d.%m %H:%M} МСК.')
    for n, item in enumerate(items, 1):
        block = f'{n}. {digest_card(item)}'
        checked = timestamp(item['card']['checked_at']).astimezone(MOSCOW)
        if item.get('needs_recheck'):
            block += f'\n🕒 Нужна перепроверка. Последняя проверенная цена: {item["price"]:g} ₽, {checked:%d.%m %H:%M} МСК.'
            block += '\n' + item['recheck_reason']
        else:
            block += f'\n✅ Цена и доступность проверены {checked:%d.%m %H:%M} МСК.'
            today_events = [e for e in state.get('pending_events', []) if e['id'] == item['id']
                            and timestamp(e['at']).astimezone(MOSCOW).date().isoformat() == date]
            drops = [e for e in today_events if e['type'] == 'price_down' and e['price'] == item['price']]
            if drops:
                block += f"\nСегодня дешевле: {drops[-1]['previous_price']:g} → {item['price']:g} ₽."
        block += '\n' + item['url']
        similar = similar_used(item, state, now)
        if similar:
            block += '\n' + similar
        trend = price_history_line(item, history.get(item['id'], []))
        if trend:
            block += '\n' + trend
        blocks.append(block)
    if not fresh:
        blocks.append('Нет вариантов со свежей проверкой цены и доступности за последние 3 часа. '
                      'Это не означает, что предложений на рынке нет.')
    manual = manual_items(state, now)
    if manual:
        blocks.append('🔎 Спорные характеристики — отдельно от топа\n'
                      'Соответствие требованиям не подтверждено; уточнить у продавца.')
        for item in manual:
            checked = timestamp(item['card']['checked_at']).astimezone(MOSCOW)
            reasons = '; '.join(item['flags']['rejection_reasons'])[:600]
            blocks.append(f'{item["title"][:100]}\nПоследняя проверенная цена: {item["price"]:g} ₽ '
                          f'({checked:%d.%m %H:%M} МСК).\n'
                          f'{hardware(item)}\nУточнить: {reasons}\n{item["url"]}')
    blocks.append(f"Последнее обновление базы: {timestamp(state.get('updated')).astimezone(MOSCOW):%d.%m %H:%M} МСК."
                  + (' Проверка карточек завершена частично.' if state.get('collection_status') == 'partial' else ''))
    return blocks, items


def deliver_digest(state, folder, now, sender=None, delivery_id=None):
    sender = sender or notify.send
    values = notify.config()
    if not notify.ready(values):
        return {'status': 'not_configured', 'sent': 0}
    date = timestamp(now).astimezone(MOSCOW).date().isoformat()
    if delivery_id and not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', delivery_id):
        raise ValueError('invalid_delivery_id')
    path = folder / (f'manual-digest-{delivery_id}.json' if delivery_id else f'digest-{date}.json')
    delivery = read(path, None)
    if delivery is None:
        blocks, items = digest_blocks(state, now, read(folder / 'paused.json', None), folder / 'history.jsonl')
        parts, groups = pack_digest(blocks, items)
        delivery = {'parts': parts, 'part_blocks': groups, 'sent_parts': 0,
                    'ids': [x['id'] for x in items]}
        save(path, delivery)  # Freeze the exact digest before delivery; retries continue it.
    count = 0
    for index in range(delivery['sent_parts'], len(delivery['parts'])):
        part = delivery['parts'][index]
        markup = None
        if index < len(delivery.get('part_blocks', [])):
            blocks = [block for block in delivery['part_blocks'][index]
                      if block['id'] is None or (not hidden(state, block['id'])
                                                and block['price'] >= MIN_PRICE)]
            part = '\n\n'.join(block['text'] for block in blocks)
            markup = digest_keyboard(blocks)
        if not part:
            delivery['sent_parts'] += 1; save(path, delivery)
            continue
        try:
            if markup:
                sender(part, values, reply_markup=markup)
            else:
                sender(part, values)
        except Exception as exc:
            return {'status': 'error', 'error_type': type(exc).__name__, 'sent': count}
        delivery['sent_parts'] += 1; save(path, delivery); count += 1
    shown = set(read(folder / 'shown_ids.json', [])) | set(delivery['ids'])
    save(folder / 'shown_ids.json', sorted(shown))
    return {'status': 'ok', 'sent': count, 'items': len(delivery['ids'])}


def main():
    parser = argparse.ArgumentParser(description='Offline digest preview unless --scheduled is used')
    parser.add_argument('--scheduled', action='store_true')
    parser.add_argument('--send-now', action='store_true', help='Send an explicitly requested updated digest')
    parser.add_argument('--delivery-id', help='Unique receipt ID; reuse it only to finish unsent parts')
    args = parser.parse_args()
    if args.scheduled and args.send_now:
        parser.error('--scheduled and --send-now are mutually exclusive')
    if bool(args.send_now) != bool(args.delivery_id):
        parser.error('--send-now requires --delivery-id (and vice versa)')
    os.umask(0o077)
    now = dt.datetime.now(MOSCOW)
    with (STATE / 'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = read(STATE / 'state.json', {'seen': {}})
        if args.scheduled or args.send_now:
            if args.scheduled and (now.hour != 20 or now.minute > 15):
                print('Outside digest window'); return
            from recovery import deliver_health
            if args.scheduled:
                deliver_health(STATE)
            result = deliver_digest(state, STATE, now.isoformat(), delivery_id=args.delivery_id)
            save(STATE / 'digest_status.json', result)
            print(json.dumps(result))
            return 0 if result['status'] == 'ok' else 1
        blocks, _ = digest_blocks(state, now.isoformat(), read(STATE / 'paused.json', None))
        (STATE / 'digest-preview.txt').write_text('\n\n'.join(blocks) + '\n')
        print('Offline preview: results/monitor/digest-preview.txt; no messages sent')


if __name__ == '__main__':
    raise SystemExit(main())
