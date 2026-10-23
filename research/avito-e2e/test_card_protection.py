import json
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import run_once as runner

URL = 'https://www.avito.ru/kazan/noutbuki/laptop_123'


def response(status, text='', payload=None, headers=None):
    return SimpleNamespace(status_code=status, text=text, headers=headers or {},
                           json=lambda: payload or {}, url=URL)


@pytest.fixture(autouse=True)
def setup():
    runner.configure()
    runner.BoundedSession.events = []
    runner.BoundedSession.deadline = time.monotonic() + 150


@pytest.mark.parametrize('status', [429, 439])
@pytest.mark.parametrize('ttl', [60, None])
def test_pow_same_session_url_headers_and_private_diagnostics(monkeypatch, status, ttl):
    challenge = response(status, payload={'pow_challenge': 'SECRET'})
    ok = response(200, '<h1>Laptop</h1>')
    session = Mock(); session.get.side_effect = [challenge, ok]
    handler = Mock(return_value=ttl)
    monkeypatch.setattr(runner.upstream, 'handle_firewall_response', handler)
    actual, diagnostic = runner.fetch_card(session, URL)
    assert actual is ok and not diagnostic['blocked']
    assert diagnostic['transitions'] == 1
    assert [x['status'] for x in diagnostic['attempts']] == [status, 200]
    assert all(c.args == (URL,) for c in session.get.call_args_list)
    handler.assert_called_once_with(session, challenge, context='card')
    assert 'SECRET' not in json.dumps(diagnostic)
    assert session.headers.update.call_count == 2


def test_qrator_then_pow_same_card(monkeypatch):
    qrator = response(302, '<meta http-equiv="refresh"><script src="/12345678901234567890.js"></script>',
                      headers={'server': 'QRATOR'})
    session = Mock()
    session.get.side_effect = [qrator, response(439, payload={'pow_challenge': 'private'}), response(200)]
    flow = Mock()
    monkeypatch.setattr(runner.upstream, 'run_qrator_cookie_flow', flow)
    monkeypatch.setattr(runner.upstream, 'handle_firewall_response', Mock(return_value=60))
    actual, diagnostic = runner.fetch_card(session, URL)
    assert actual.status_code == 200 and diagnostic['transitions'] == 2
    flow.assert_called_once_with(session, qrator_html=qrator.text, page_url=URL, context='card')


def test_unknown_429_stops_without_solver(monkeypatch):
    session = Mock(); session.get.return_value = response(429, 'unknown SECRET')
    handler = Mock(); monkeypatch.setattr(runner.upstream, 'handle_firewall_response', handler)
    _, diagnostic = runner.fetch_card(session, URL)
    assert diagnostic['stop_reason'] == 'unsupported_protection'
    assert session.get.call_count == 1
    handler.assert_not_called()
    assert 'SECRET' not in json.dumps(diagnostic)


def test_repeated_pow_is_bounded(monkeypatch):
    session = Mock(); session.get.return_value = response(439, payload={'pow_challenge': 'private'})
    handler = Mock(return_value=60); monkeypatch.setattr(runner.upstream, 'handle_firewall_response', handler)
    _, diagnostic = runner.fetch_card(session, URL)
    assert diagnostic['blocked'] and diagnostic['stop_reason'] == 'protection_limit'
    assert handler.call_count == 3 and session.get.call_count == 4


def test_dispatcher_failure_does_not_retry_or_expose_error(monkeypatch):
    session = Mock()
    session.get.return_value = response(429, payload={'too-many-requests': {'link': 'ru.avito://1/firewall/captcha/show'}},
                                        headers={'x-firewall-show-captcha': 'true'})
    handler = Mock(side_effect=RuntimeError('secret token'))
    monkeypatch.setattr(runner.upstream, 'handle_firewall_response', handler)
    _, diagnostic = runner.fetch_card(session, URL)
    assert diagnostic['attempts'][0]['protection'] == 'captcha_dispatcher'
    assert diagnostic['error_type'] == 'RuntimeError' and diagnostic['blocked']
    assert session.get.call_count == 1 and 'secret' not in json.dumps(diagnostic)


def test_successful_dispatcher_is_not_repeated_indefinitely(monkeypatch):
    session = Mock()
    session.get.return_value = response(429, payload={'too-many-requests': {'link': 'ru.avito://1/firewall/captcha/show'}},
                                        headers={'x-firewall-show-captcha': 'true'})
    handler = Mock(return_value=object())
    monkeypatch.setattr(runner.upstream, 'handle_firewall_response', handler)
    _, diagnostic = runner.fetch_card(session, URL)
    assert diagnostic['stop_reason'] == 'protection_limit'
    assert handler.call_count == 1 and session.get.call_count == 2


def test_html_block_with_200_is_not_parsed_as_card(monkeypatch):
    session = Mock(); session.get.return_value = response(200, '<h1>Доступ ограничен</h1>')
    handler = Mock(); monkeypatch.setattr(runner.upstream, 'handle_firewall_response', handler)
    _, diagnostic = runner.fetch_card(session, URL)
    assert diagnostic['blocked'] and diagnostic['stop_reason'] == 'unsupported_protection'
    handler.assert_not_called()


def test_budget_stops_before_protection(monkeypatch):
    session = Mock(); session.get.return_value = response(439, payload={'pow_challenge': 'private'})
    runner.BoundedSession.events = [{}] * (runner.REQUEST_LIMIT - 3)
    handler = Mock(); monkeypatch.setattr(runner.upstream, 'handle_firewall_response', handler)
    _, diagnostic = runner.fetch_card(session, URL)
    assert diagnostic['stop_reason'] == 'request_or_time_budget'
    handler.assert_not_called()


@pytest.mark.parametrize('status', [200, 404, 410])
def test_regular_card_and_closed_pages_not_challenges(status):
    session = Mock(); session.get.return_value = response(status, '<h1>Laptop</h1>')
    actual, diagnostic = runner.fetch_card(session, URL)
    assert actual.status_code == status and not diagnostic['blocked']
    assert session.get.call_count == 1
