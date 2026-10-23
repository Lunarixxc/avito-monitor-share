import copy
import datetime as dt
import json
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest
import notify
from filters import classify, specifications
from lifecycle import reconcile
from notifications import (deliver_events, deliver_digest, digest_blocks, digest_card,
                           price_history_line, similar_used, verified_price_history)
from ranking import eligible, market_top, top_items
from tracking import page_facts, review_queue
from monitor import scheduled_window
from preferences import change, hidden, listing_id

NOW = '2026-09-22T16:30:00+00:00'


def listing(identifier='1', price=45000):
    return {'id': identifier, 'title': 'MSI RTX 3050', 'urlPath': f'/kazan/noutbuki/msi_{identifier}',
            'priceDetailed': {'value': price}}


def card(identifier='1', price=45000, status='active', at=NOW):
    return {'id': identifier, 'parsed': status == 'active', 'availability': status,
            'price': price if status == 'active' else None, 'reviewed_price': price if status == 'active' else None,
            'price_source': 'card' if status == 'active' else 'unknown', 'checked_at': at,
            'parameters': 'Видеокарта: RTX 3050 Объем видеопамяти, ГБ: 4 Оперативная память, ГБ: 16',
            'description': 'Один ноутбук', 'listing': listing(identifier, price)}


def snapshot(cards=None, listings=None, at=NOW):
    return {'started': at, 'status': 'success', 'cards': cards or [],
            'listings': [listing()] if listings is None else listings}


def active_state():
    state, _ = reconcile({}, snapshot([card()]))
    return state


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(notify, 'config', lambda: {'TELEGRAM_ENABLED': '1', 'BOT_TOKEN': '1:test', 'TELEGRAM_CHAT_ID': '1'})


def test_price_read_from_card_not_search_and_true_drop():
    previous = active_state()
    state, report = reconcile(previous, snapshot([card(price=40000)]))
    assert state['seen']['1']['price'] == 40000
    assert state['seen']['1']['search_price'] == 45000
    assert report['events'][0]['type'] == 'price_down'
    assert report['events'][0]['previous_price'] == 45000
    assert previous['seen']['1']['price'] == 45000
    _, again = reconcile(state, snapshot([card(price=40000)]))
    assert again['events'] == []


def test_no_sale_on_search_absence_or_single_404_and_return():
    state = active_state()
    state, report = reconcile(state, snapshot(listings=[]))
    assert not report['events'] and state['seen']['1']['availability'] == 'active'
    state, report = reconcile(state, snapshot([card(status='missing')], listings=[]))
    assert state['seen']['1']['availability'] == 'unknown' and not report['events']
    state, report = reconcile(state, snapshot([card(status='missing')], listings=[], at='2026-09-22T17:00:00Z'))
    assert state['seen']['1']['availability'] == 'unavailable'
    assert report['events'][0]['type'] == 'unavailable'
    state, report = reconcile(state, snapshot([card()], at='2026-09-22T17:30:00Z'))
    assert report['events'][0]['type'] == 'returned'


def test_replayed_missing_snapshot_does_not_count_twice():
    state, _ = reconcile(active_state(), snapshot([card(status='missing')]))
    state, report = reconcile(state, snapshot([card(status='missing')]))
    assert state['seen']['1']['availability'] == 'unknown' and not report['events']


def test_explicit_sale_is_event_but_blocking_is_unknown():
    state, report = reconcile(active_state(), snapshot([card(status='sold')]))
    assert report['events'][0]['type'] == 'sold'
    assert not report['candidates']
    state, report = reconcile(active_state(), snapshot([card(status='blocked')]))
    assert state['seen']['1']['availability'] == 'unknown' and not report['events']


def test_page_price_scope_and_missing_price():
    html = '<h1>MSI</h1><meta itemprop="price" content="44200"><div data-marker="item-view/item-description">Другой уже продано за 10000</div>'
    assert page_facts(html)['price'] == 44200
    assert page_facts(html)['availability'] == 'active'
    assert page_facts('<h1>MSI</h1><p>Цена 44200</p>')['price'] is None
    assert page_facts('<h1>Объявление снято с публикации</h1>')['availability'] == 'removed'
    assert page_facts('<h1>MSI</h1><div data-marker="item-view/item-status">Продано</div>')['availability'] == 'sold'
    assert page_facts('<h1>Доступ ограничен</h1>')['availability'] == 'blocked'
    assert page_facts('', 404)['availability'] == 'missing'
    assert page_facts('<h1>MSI</h1><meta itemprop="price" content="44000"><div data-marker="item-view/item-price">52 000 ₽</div>')['price'] is None


def test_manual_specs_applied_without_forging_price_or_read():
    assert specifications({'id': '8274410405'})['vram_gb'] == 4
    assert specifications({'id': '8344920525'})['vram_gb'] == 4
    flags = classify(listing('8314471042'))
    assert not flags['candidate'] and '1650' in flags['specs']['gpu']
    state, _ = reconcile({}, snapshot(listings=[listing('8395453202', 52000)]))
    assert state['seen']['8395453202']['price'] == 52000
    assert not state['seen']['8395453202']['flags']['notify_ready']


def test_queue_checks_known_urls_outside_search_and_new_candidates():
    state = active_state()
    queue = review_queue([listing('2'), listing('3')], state['seen'], 2, now=NOW)
    assert {str(x['id']) for x in queue} == {'1', '2'}


def test_digest_freshness_and_limit():
    state = active_state()
    assert len(top_items(state, NOW)) == 1
    assert not top_items(state, '2026-09-23T00:00:00Z')
    stale = copy.deepcopy(state)
    stale['seen']['1']['card']['price_source'] = 'legacy_search'
    assert not top_items(stale, NOW)
    blocks, items = digest_blocks(state, NOW)
    assert len(items) == 1 and 'RTX 3050 4 GB' in '\n'.join(blocks)
    assert '1 из максимум 10' in blocks[0]


def test_digest_shows_table_percent_and_only_verified_price_history(tmp_path, configured):
    from analysis import assess, load_reference
    state = active_state()
    item = state['seen']['1']
    item['analysis'] = assess(item, [item], load_reference(), NOW)
    history_path = tmp_path / 'history.jsonl'
    older = '2026-09-21T16:30:00+00:00'
    def row(at, price, source='card'):
        return {'snapshot': at, 'candidates': [{
            'id': '1', 'price': price,
            'card': {'parsed': True, 'price_source': source,
                     'reviewed_price': price, 'checked_at': at}}]}
    history_path.write_text('\n'.join(json.dumps(x) for x in (
        row(older, 50000), row('2026-09-22T15:00:00+00:00', 10000, 'search'),
        row(NOW, 45000))) + '\n')
    observed = verified_price_history(history_path, ['1'])['1']
    assert [price for _, price in observed] == [50000, 45000]
    blocks, _ = digest_blocks(state, NOW, history_path=history_path)
    rendered = '\n'.join(blocks)
    assert 'По таблице новых от 20.09: дешевле на' in rendered
    assert 'История с 21.09: 50 000 → 45 000 ₽ (-10,0%)' in rendered
    assert item['url'] in next(block for block in blocks if block.startswith('1. '))
    assert 'По вашим приоритетам №1' in rendered
    state['pending_events'] = [{'event_id': 'price-1', 'id': '1', 'type': 'price_down',
                                'at': NOW, 'price': 45000, 'previous_price': 50000}]
    (tmp_path / 'shown_ids.json').write_text('["1"]')
    sender = Mock()
    assert deliver_events(state, tmp_path, NOW, sender)['sent'] == 1
    assert 'К новым по вашей таблице' in sender.call_args.args[0]


def test_evening_card_and_gpu_only_peers():
    state = active_state()
    item = state['seen']['1']
    item['flags']['seller'] = {'type': 'private'}
    item['card']['parameters'] += (' Процессор: Intel Core i5-12500H'
                                   ' Конфигурация накопителей: SSD Объем накопителей, ГБ: 512')
    from analysis import assess, load_reference
    item['analysis'] = assess(item, [item], load_reference(), NOW)
    card_text = digest_card(item)
    assert 'MSI RTX 3050 — 45 000 ₽' in card_text
    assert 'Видеокарта: RTX 3050 4 GB' in card_text
    assert 'Процессор: Intel Core i5-12500H' in card_text
    assert 'Оперативка: 16 ГБ' in card_text
    assert 'Память: SSD — 512 ГБ' in card_text
    assert 'Продавец: Частное лицо' in card_text
    assert 'По таблице новых от 20.09: дешевле на' in card_text
    for identifier, gpu, price in [('2', 'RTX 3050', 42000), ('3', 'RTX 3050 Ti', 40000),
                                    ('4', 'RTX 4050', 39000), ('5', 'RTX 3050', 43000)]:
        peer = copy.deepcopy(item)
        peer['id'] = identifier
        peer['title'] = f'Ноутбук {identifier}'
        peer['url'] = f'https://www.avito.ru/kazan/noutbuki/{identifier}'
        peer['price'] = price
        peer['card']['reviewed_price'] = price
        peer['card']['parameters'] = peer['card']['parameters'].replace('RTX 3050', gpu)
        state['seen'][identifier] = peer
    state['seen']['2']['price'] = 38000  # Search price differs; show the verified card price.
    state['seen']['5']['availability'] = 'sold'
    expensive = copy.deepcopy(state['seen']['2'])
    expensive.update(id='6', title='Ноутбук 6', url='https://www.avito.ru/kazan/noutbuki/6', price=70000)
    expensive['card']['reviewed_price'] = 70000
    expensive['flags'].update(candidate=False, notify_ready=False, within_budget=False)
    state['seen']['6'] = expensive
    text = similar_used(item, state, NOW)
    assert 'Ноутбук 2 — 42 000 ₽\nhttps://www.avito.ru/kazan/noutbuki/2' in text
    assert 'Ноутбук 6 — 70 000 ₽\nhttps://www.avito.ru/kazan/noutbuki/6' in text
    assert 'Ноутбук 3' not in text and 'Ноутбук 4' not in text and 'Ноутбук 5' not in text
    blocks, _ = digest_blocks(state, NOW)
    assert text in next(block for block in blocks if block.split('\n', 1)[0].endswith('MSI RTX 3050 — 45 000 ₽'))
    assert 'Продавец: Неизвестно' in digest_card({**item, 'flags': {}})
    assert 'Продавец: Магазин' in digest_card({**item, 'flags': {'seller': {'type': 'shop'}}})


def test_price_history_only_after_an_actual_change():
    item = active_state()['seen']['1']
    old = '2026-09-21T16:30:00+00:00'
    assert not price_history_line(item, [(old, 45000), (NOW, 45000)])
    assert price_history_line(item, [(old, 50000), (NOW, 45000)]).startswith('История с 21.09:')


def test_event_delivery_retry_dedup_and_batching(tmp_path, configured):
    state = active_state()
    sender = Mock(side_effect=ValueError('secret body'))
    assert deliver_events(state, tmp_path, NOW, sender)['status'] == 'error'
    assert not (tmp_path / 'event_receipts.json').exists()
    sender = Mock()
    assert deliver_events(state, tmp_path, NOW, sender)['sent'] == 1
    assert deliver_events(state, tmp_path, NOW, sender)['sent'] == 0
    state, _ = reconcile(state, snapshot([card(price=40000)]))
    assert deliver_events(state, tmp_path, NOW, sender)['sent'] == 1
    assert '45000 → 40000' in sender.call_args.args[0]
    assert deliver_events(state, tmp_path, NOW, sender)['sent'] == 0


def test_removal_alert_only_for_shown_items(tmp_path, configured):
    state, _ = reconcile(active_state(), snapshot([card(status='removed')]))
    sender = Mock()
    assert deliver_events(state, tmp_path, NOW, sender)['sent'] == 0
    (tmp_path / 'shown_ids.json').write_text('["1"]')
    assert deliver_events(state, tmp_path, NOW, sender)['sent'] == 1
    assert 'Снят с публикации' in sender.call_args.args[0]


def test_digest_once_per_date(tmp_path, configured):
    sender = Mock()
    state = active_state()
    assert deliver_digest(state, tmp_path, NOW, sender)['sent'] == 1
    assert deliver_digest(state, tmp_path, NOW, sender)['sent'] == 0
    assert json.loads((tmp_path / 'shown_ids.json').read_text()) == ['1']


def test_schedule_daytime_no_collision_with_digest():
    def at(h, m):
        return dt.datetime(2026, 9, 22, h, m, tzinfo=ZoneInfo('Europe/Moscow'))
    for h, m in [(9, 0), (11, 0), (13, 0), (15, 0), (17, 0), (19, 0), (21, 0)]:
        assert scheduled_window(at(h, m))
    for h, m in [(8, 30), (9, 30), (10, 0), (19, 30), (20, 0), (20, 30), (21, 30), (22, 0), (10, 20)]:
        assert not scheduled_window(at(h, m))


def test_known_defect_stays_out_of_events_and_fresh_or_stale_top(tmp_path, configured):
    state = active_state()
    item = state['seen']['1']
    item['card']['description'] = 'Не функционируют четыре клавиши'
    sender = Mock()
    assert not eligible(item, NOW)
    assert not market_top(state, NOW)
    assert not market_top(state, '2026-09-22T20:30:00+00:00')
    assert deliver_events(state, tmp_path, NOW, sender)['sent'] == 0
    sender.assert_not_called()


def test_closed_watch_does_not_starve_discovery():
    state, _ = reconcile(active_state(), snapshot([card(status='removed')]))
    queue = review_queue([listing(), listing('2')], state['seen'], 2, now=NOW)
    assert [x['id'] for x in queue] == ['2']


def test_digest_resumes_remaining_parts(tmp_path, configured):
    state = active_state()
    original = state['seen']['1']
    for n in range(2, 11):
        entry = copy.deepcopy(original); entry['id'] = str(n)
        entry['title'] = 'Ноутбук ' + ('длинное название ' * 8)
        entry['url'] = 'https://www.avito.ru/kazan/noutbuki/' + ('igrovoy_noutbuk_' * 8) + str(n)
        state['seen'][str(n)] = entry
    sender = Mock(side_effect=[None, RuntimeError('timeout')])
    result = deliver_digest(state, tmp_path, NOW, sender)
    assert result['status'] == 'error' and result['sent'] == 1
    record = json.loads((tmp_path / 'digest-2026-09-22.json').read_text())
    assert record['sent_parts'] == 1 and len(record['parts']) > 1
    sender = Mock()
    result = deliver_digest(state, tmp_path, NOW, sender)
    assert result['sent'] == len(record['parts']) - 1
    assert sender.call_args_list[0].args[0] == record['parts'][1]


def test_hide_excludes_top_and_peers_until_verified_price_change(tmp_path):
    state = active_state()
    peer = copy.deepcopy(state['seen']['1'])
    peer['id'] = '2'; peer['title'] = 'Другой ноутбук'
    peer['url'] = 'https://www.avito.ru/kazan/noutbuki/other_2'
    state['seen']['2'] = peer
    (tmp_path / 'state.json').write_text(json.dumps(state))
    assert change(tmp_path, '1', 'hide', NOW, expected_price=45000) == 'hidden'
    state = json.loads((tmp_path / 'state.json').read_text())
    assert hidden(state, '1')
    assert [x['id'] for x in market_top(state, NOW)] == ['2']
    assert 'MSI RTX 3050' not in similar_used(state['seen']['2'], state, NOW)
    search_only = snapshot(listings=[listing(price=40000)])
    state, _ = reconcile(state, search_only)
    assert hidden(state, '1') and not any(x['id'] == '1' for x in market_top(state, NOW))
    changed = snapshot([card(price=40000)], listings=[listing(price=40000)],
                       at='2026-09-22T17:30:00+00:00')
    state, report = reconcile(state, changed)
    assert not hidden(state, '1')
    assert any(e['type'] == 'price_down' and e['id'] == '1' for e in report['events'])
    assert any(x['id'] == '1' for x in market_top(state, changed['started']))


def test_hide_command_validation_and_stale_button(tmp_path):
    state = active_state()
    (tmp_path / 'state.json').write_text(json.dumps(state))
    assert listing_id('https://www.avito.ru/kazan/noutbuki/msi_123?foo=1') == '123'
    assert listing_id('https://evil.example/kazan/noutbuki/msi_123') is None
    assert change(tmp_path, '1', 'hide', NOW, expected_price=40000) == 'stale'
    assert change(tmp_path, '1', 'hide', NOW) == 'hidden'
    assert change(tmp_path, '1', 'hide', NOW) == 'already_hidden'
    assert change(tmp_path, '1', 'show', NOW) == 'visible'
    assert not json.loads((tmp_path / 'state.json').read_text())['hidden']


def test_hide_clears_on_verified_increase_outside_budget():
    state = active_state()
    state['hidden'] = {'1': {'price': 45000, 'at': NOW}}
    later = '2026-09-22T17:30:00+00:00'
    state, _ = reconcile(state, snapshot([card(price=70000, at=later)],
                                         listings=[listing(price=70000)], at=later))
    assert not hidden(state, '1')


def test_hide_applies_to_queued_digest_part(tmp_path, configured):
    state = active_state()
    original = state['seen']['1']
    for n in range(2, 11):
        item = copy.deepcopy(original); item['id'] = str(n)
        item['title'] = 'Ноутбук ' + ('длинное название ' * 8)
        item['url'] = 'https://www.avito.ru/kazan/noutbuki/' + ('igrovoy_noutbuk_' * 8) + str(n)
        state['seen'][str(n)] = item
    sender = Mock(side_effect=[None, RuntimeError('timeout')])
    assert deliver_digest(state, tmp_path, NOW, sender)['sent'] == 1
    record = json.loads((tmp_path / 'digest-2026-09-22.json').read_text())
    queued = record['part_blocks'][1]
    hide_id = next(block['id'] for block in queued if block['id'])
    state['hidden'] = {hide_id: {'price': 45000, 'at': NOW}}
    sender = Mock()
    assert deliver_digest(state, tmp_path, NOW, sender)['status'] == 'ok'
    sent_text = '\n'.join(call.args[0] for call in sender.call_args_list)
    assert next(block['text'] for block in queued if block['id'] == hide_id) not in sent_text


def test_hidden_item_suppresses_pending_events_and_digest_button(tmp_path, configured):
    state = active_state()
    state['hidden'] = {'1': {'price': 45000, 'at': NOW}}
    sender = Mock()
    assert deliver_events(state, tmp_path, NOW, sender)['sent'] == 0
    state['hidden'].clear()
    assert deliver_digest(state, tmp_path, NOW, sender)['sent'] == 1
    markup = sender.call_args.kwargs['reply_markup']
    assert markup['inline_keyboard'][0][0]['callback_data'] == 'hide:1:45000'


def test_model_specs_do_not_override_manual_or_structured_fields():
    actual = card('8344920525')
    actual['semantic_specs'] = {'vram_gb': 8, 'gpu': 'RTX 4090', 'cpu': 'Intel Core i5-11400H'}
    specs = specifications(actual)
    assert specs['vram_gb'] == 4 and specs['gpu'] == 'RTX 3050'
    assert specs['cpu'] == 'Intel Core i5-11400H'
