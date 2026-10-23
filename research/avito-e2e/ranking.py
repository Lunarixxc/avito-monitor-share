"""Transparent preference ranking; never claims measured GPU performance."""
from analysis import risk_signals
from price_signals import strong_price
from filters import specifications, MIN_PRICE
from tracking import timestamp
from copy import deepcopy
from preferences import hidden


def eligible(item, now, max_age_hours=3):
    card = item.get('card') or {}
    age = (timestamp(now) - timestamp(card.get('checked_at'))).total_seconds()
    return (item.get('flags', {}).get('notify_ready') and item.get('availability') == 'active'
            and item['price'] >= MIN_PRICE
            and card.get('price_source') == 'card' and card.get('reviewed_price') == item['price']
            and 0 <= age <= max_age_hours * 3600
            and not any(r['code'] == 'defect' for r in risk_signals(card)))


def rank(item):
    specs = specifications({**(item.get('card') or {}), 'id': item['id']})
    gpu = specs['gpu'].upper()
    score = max(0, 20 * (65000 - item['price']) / 65000)
    reasons = []
    if '4050' in gpu:
        score += 20; reasons.append('предпочтительная RTX 4050')
    elif '2070' in gpu:
        score += 15; reasons.append('производительный класс RTX 2070')
    elif any(x in gpu for x in ('3060', '3070', '4060', '4070')):
        score += 15; reasons.append('конфигурация GPU выше минимального фильтра')
    else:
        score += 8; reasons.append('соответствует фильтру RTX 3050+')
    if item['flags'].get('seller', {}).get('type') == 'private':
        score += 5; reasons.append('частный продавец по профилю')
    for key in ('cpu', 'ram', 'storage'):
        if str(specs[key]).startswith('не указан'):
            score -= 4
    if not specs.get('vram_gb'):
        score -= 4
    import re
    ram = re.search(r"\d+", specs['ram'])
    if ram and int(ram[0]) >= 16:
        score += 3
    condition = (item.get('analysis') or {}).get('facts', {}).get('condition_detail')
    if condition == 'отличное':
        score += 3
    elif condition == 'удовлетворительное':
        score -= 8
    risks = (item.get('analysis') or {}).get('risk_signals', [])
    serious = [r for r in risks if r['code'] in ('repair', 'defect')]
    score -= 25 * len(serious)
    score -= 8 * sum(r['code'] == 'peripheral' for r in risks)
    # Only an actually sufficient sample can contribute a market discount.
    used = (item.get('analysis') or {}).get('used_comparison', {})
    if used.get('sufficient'):
        score += max(-15, min(15, -(used.get('difference_pct') or 0)))
    if not item.get('needs_recheck') and strong_price(item):
        score += 20
        reasons.insert(0, 'ТОП-цена по ориентиру владельца')
    caveat = (serious[0]['label'] + ': «' + serious[0]['evidence'][:120] + '»') if serious else ''
    if not caveat:
        peripheral = next((r for r in risks if r['code'] == 'peripheral'), None)
        if peripheral:
            caveat = peripheral['label']
    if condition == 'удовлетворительное' and not serious:
        caveat = 'заявлено удовлетворительное состояние'
    if not caveat and specs['cpu'].startswith('не указан'):
        caveat = 'процессор не указан в характеристиках'
    if not specs.get('vram_gb'):
        caveat = 'объём видеопамяти не подтверждён'
    return {'score': round(score, 2), 'reason': '; '.join(reasons[:2]), 'caveat': caveat}


def top_items(state, now, limit=10):
    entries = [x for x in state.get('seen', {}).values() if not hidden(state, x['id']) and eligible(x, now)]
    return sorted(entries, key=lambda x: (-rank(x)['score'], x['price'], x['id']))[:limit]


def recent_card(item, now, max_age_hours=72):
    card = item.get('card') or {}
    age = (timestamp(now) - timestamp(card.get('checked_at'))).total_seconds()
    return (card.get('parsed') and card.get('price_source') == 'card'
            and type(card.get('reviewed_price')) in (int, float)
            and card['reviewed_price'] >= MIN_PRICE and 0 <= age <= max_age_hours * 3600
            and item.get('availability') not in ('sold', 'removed', 'unavailable')
            and timestamp(item.get('last_missing_at')) < timestamp(card.get('checked_at'))
            and not any(r['code'] == 'defect' for r in risk_signals(card)))


def stale_items(state, now, limit=10):
    """Previously qualified cards; never label their old price as current."""
    entries = []
    for item in state.get('seen', {}).values():
        if (not hidden(state, item['id']) and item.get('flags', {}).get('notify_ready') and recent_card(item, now)
                and not eligible(item, now)):
            entry = deepcopy(item)
            entry['price'] = entry['card']['reviewed_price']
            entry['needs_recheck'] = True
            if item['price'] != entry['price']:
                entry['recheck_reason'] = f'Цена выдачи {item["price"]:g} ₽ расходится с последней ценой карточки.'
            elif item.get('availability') == 'unknown':
                entry['recheck_reason'] = 'Последняя попытка не подтвердила доступность карточки.'
            else:
                entry['recheck_reason'] = 'Проверка карточки старше трёх часов.'
            entries.append(entry)
    return sorted(entries, key=lambda x: (-rank(x)['score'], x['price'], x['id']))[:limit]


def market_top(state, now, limit=10):
    """One ranking across the known inventory, independent of discovery date."""
    entries = top_items(state, now, limit) + stale_items(state, now, limit)
    return sorted(entries, key=lambda x: (-rank(x)['score'], x['price'], x['id']))[:limit]


def manual_items(state, now, limit=3):
    """Configuration contradictions stay visible, but never qualify for the top."""
    from filters import gpu_models, GPU_MODELS
    import re
    entries = []
    for item in state.get('seen', {}).values():
        if hidden(state, item['id']):
            continue
        flags = item.get('flags', {})
        reasons = flags.get('rejection_reasons', [])
        specs = specifications({**(item.get('card') or {}), 'id': item['id']})
        models = gpu_models(specs['gpu'])
        if not (recent_card(item, now) and flags.get('kazan_url')
                and flags.get('within_budget') and not flags.get('excluded_title')
                and not flags.get('bulk_offer') and flags.get('condition') != 'new'
                and len(models) == 1 and models[0].startswith('RTX ')
                and int(re.search(r'\d+', models[0])[0]) in GPU_MODELS and reasons):
            continue
        # Do not surface assortments, absent GPU, or unsupported hardware as choices.
        if not all(('отличается от характеристик' in reason or
                    reason.startswith('Требует ручной проверки по разбору DeepSeek:'))
                   for reason in reasons):
            continue
        risks = ((item.get('card') or {}).get('semantic_analysis') or {}).get('payload', {}).get('risks', [])
        if any(r['code'] == 'assortment' for r in risks):
            continue
        entry = deepcopy(item)
        entry['price'] = entry['card']['reviewed_price']
        entries.append(entry)
    return sorted(entries, key=lambda x: (-rank(x)['score'], x['price'], x['id']))[:limit]
