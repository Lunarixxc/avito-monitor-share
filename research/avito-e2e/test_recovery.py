import copy
import datetime as dt
import json
from unittest.mock import Mock

import notify
import recovery
from notifications import digest_blocks, deliver_digest
from ranking import stale_items, manual_items, market_top
from tracking import review_queue
from test_workflow import NOW, active_state, listing
from test_workflow import card, snapshot


def test_cooldown_legacy_escalation_cap_and_recovery(tmp_path):
    legacy = {'at': NOW, 'reason': 'details_blocked'}
    recovery.save(tmp_path / 'paused.json', legacy)
    now = dt.datetime.fromisoformat(NOW)
    assert not recovery.retry_due(legacy, NOW)
    assert recovery.retry_due(legacy, (now + dt.timedelta(hours=2)).isoformat())
    for hours in (2, 4, 8, 16, 24, 24):
        pause = recovery.fail(tmp_path, now.isoformat(), 'HTTP 429')
        assert recovery.retry_at(pause) == now + dt.timedelta(hours=hours)
        assert not recovery.retry_due(pause, now.isoformat())
        now += dt.timedelta(hours=hours)
        assert recovery.retry_due(pause, now.isoformat())
    recovery.recover(tmp_path, now.isoformat())
    assert not (tmp_path / 'paused.json').exists()
    assert json.loads((tmp_path / 'pause-history.jsonl').read_text())['pause']['attempts'] == 6
    assert recovery.retry_due(None, NOW)
    assert recovery.next_retry_slot({'retry_after': '2026-09-22T19:19:00Z'}).isoformat() == '2026-09-23T09:00:00+03:00'


def test_health_retry_and_no_duplicate(tmp_path, monkeypatch):
    monkeypatch.setattr(notify, 'config', lambda: {'TELEGRAM_ENABLED': '1', 'BOT_TOKEN': '1:test', 'TELEGRAM_CHAT_ID': '1'})
    recovery.fail(tmp_path, NOW, 'HTTP 429')
    assert recovery.deliver_health(tmp_path, Mock(side_effect=ValueError('private')))['status'] == 'error'
    sender = Mock()
    assert recovery.deliver_health(tmp_path, sender)['sent'] == 1
    assert recovery.deliver_health(tmp_path, sender)['sent'] == 0
    assert sender.call_count == 1


def test_scheduled_cooldown_keeps_next_two_hour_slot(tmp_path):
    started = '2026-09-27T12:00:07.553056+00:00'  # 15:00:07 Moscow time
    pause = recovery.fail(tmp_path, started, 'HTTP 429')
    assert recovery.next_retry_slot(pause).isoformat() == '2026-09-27T17:00:00+03:00'
    assert not recovery.retry_due(pause, '2026-09-27T16:59:59+03:00')
    assert recovery.retry_due(pause, '2026-09-27T17:00:00+03:00')


def test_stale_section_uses_old_card_price_excludes_closed_and_expired():
    state = active_state()
    state['seen']['1']['price'] = 40000
    later = '2026-09-23T00:00:00Z'
    values = stale_items(state, later)
    assert values[0]['price'] == 45000
    assert state['seen']['1']['price'] == 40000
    blocks, items = digest_blocks(state, later, {'at': NOW, 'reason': 'HTTP 429'})
    text = '\n'.join(blocks)
    assert len(items) == 1 and 'Свежая проверка: 0' in text
    assert 'Сбор на паузе' in text and 'текущая цена и наличие не подтверждены' in text
    assert 'Последняя проверенная цена: 45000' in text
    assert 'Цена выдачи 40000' in text
    assert not stale_items(state, '2026-09-26T00:00:00Z')
    for status in ('sold', 'removed', 'unavailable'):
        state['seen']['1']['availability'] = status
        assert not stale_items(state, later)
    state['seen']['1']['availability'] = 'unknown'
    state['seen']['1']['last_missing_at'] = NOW
    assert not stale_items(state, later)


def test_digest_priority_refreshes_best_known_before_discovery():
    state = active_state()
    for n in range(2, 8):
        item = copy.deepcopy(state['seen']['1'])
        item['id'] = str(n)
        item['price'] = 45000 - n * 1000
        item['card']['reviewed_price'] = item['price']
        item['url'] = 'https://www.avito.ru/kazan/noutbuki/msi_' + str(n)
        state['seen'][str(n)] = item
    # New catalog IDs cannot consume the refresh quota before the evening digest.
    queue = review_queue([listing('100')], state['seen'], 5,
                         now='2026-09-23T00:00:00Z', for_digest=True)
    assert [x['id'] for x in queue] == ['7', '6', '5', '4', '3']


def test_manual_contradiction_is_separate_and_assortment_excluded():
    state = active_state()
    flags = state['seen']['1']['flags']
    flags.update(candidate=False, notify_ready=False,
                 rejection_reasons=['модель GTX/RTX в описании отличается от характеристик'])
    assert len(manual_items(state, NOW)) == 1
    blocks, items = digest_blocks(state, NOW)
    assert not items and 'Спорные характеристики' in '\n'.join(blocks)
    flags['rejection_reasons'].append('в описании предлагается ассортимент или подбор')
    assert not manual_items(state, NOW)


def test_manual_send_does_not_replace_scheduled_digest(tmp_path, monkeypatch):
    monkeypatch.setattr(notify, 'config', lambda: {'TELEGRAM_ENABLED': '1', 'BOT_TOKEN': '1:test', 'TELEGRAM_CHAT_ID': '1'})
    sender = Mock()
    state = active_state()
    deliver_digest(state, tmp_path, NOW, sender)
    path = tmp_path / 'digest-2026-09-22.json'
    original = path.read_bytes()
    assert deliver_digest(state, tmp_path, NOW, sender, delivery_id='updated-1')['sent'] == 1
    assert deliver_digest(state, tmp_path, NOW, sender, delivery_id='updated-1')['sent'] == 0
    assert path.read_bytes() == original


def test_market_top_ignores_discovery_date_and_freshness_for_rank():
    state = active_state()
    old = copy.deepcopy(state['seen']['1'])
    old.update(id='2', first_seen='2026-08-01T00:00:00Z', price=40000)
    old['card'].update(checked_at='2026-09-21T16:00:00Z', reviewed_price=40000)
    state['seen']['2'] = old
    items = market_top(state, NOW)
    assert [x['id'] for x in items] == ['2', '1']
    assert items[0]['needs_recheck']
    assert market_top(state, NOW, 1)[0]['id'] == '2'
    blocks, displayed = digest_blocks(state, NOW)
    text = '\n'.join(blocks)
    assert [x['id'] for x in displayed] == ['2', '1']
    assert 'Топ из всей базы' in text and '🕒 Нужна перепроверка' in text
    assert '✅ Цена и доступность проверены' in text
    assert 'За день:' not in text
    state['seen']['2']['availability'] = 'removed'
    assert [x['id'] for x in market_top(state, NOW)] == ['1']


def test_scheduled_retry_is_bounded_and_resumes_after_valid_cards(tmp_path, monkeypatch):
    import monitor
    import semantic
    from types import SimpleNamespace
    from zoneinfo import ZoneInfo
    class Clock(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 23, 9, 0, 10, tzinfo=ZoneInfo('Europe/Moscow'))
    folder = tmp_path / 'results/monitor'
    folder.mkdir(parents=True)
    recovery.fail(folder, NOW, 'HTTP 429')
    monkeypatch.setattr(monitor, 'ROOT', tmp_path)
    monkeypatch.setattr(monitor, 'STATE', folder)
    monkeypatch.setattr(monitor.dt, 'datetime', Clock)
    monkeypatch.setattr(monitor.sys, 'argv', ['monitor.py', '--scheduled'])
    monkeypatch.setattr(semantic, 'enrich_result', lambda *a: None)
    monkeypatch.setattr(notify, 'config', lambda: {})
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        run_id = command[command.index('--run-id') + 1]
        output = tmp_path / 'results' / run_id
        output.mkdir()
        at = Clock.now().isoformat()
        recovery.save(output / 'summary.json', snapshot([card(at=at)], at=at))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(monitor.subprocess, 'run', run)
    monitor.main()
    assert len(calls) == 1
    command = calls[0]
    assert command[command.index('--pages') + 1] == '1'
    assert command[command.index('--details') + 1] == '3'
    assert '--prepare-digest' in command
    assert not (folder / 'paused.json').exists()
    assert json.loads((folder / 'state.json').read_text())['seen']['1']['availability'] == 'active'


def test_scheduled_cooldown_makes_no_network_requests(tmp_path, monkeypatch):
    import monitor
    from zoneinfo import ZoneInfo
    class Clock(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 22, 19, 0, 10, tzinfo=ZoneInfo('Europe/Moscow'))
    recovery.fail(tmp_path, NOW, 'HTTP 429')
    monkeypatch.setattr(monitor, 'STATE', tmp_path)
    monkeypatch.setattr(monitor.dt, 'datetime', Clock)
    monkeypatch.setattr(monitor.sys, 'argv', ['monitor.py', '--scheduled'])
    monkeypatch.setattr(notify, 'config', lambda: {})
    run = Mock()
    monkeypatch.setattr(monitor.subprocess, 'run', run)
    assert monitor.main() == 0
    run.assert_not_called()
