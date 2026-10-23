from unittest.mock import Mock

import pytest

import notify
from filters import choose_details, description_warnings


def candidate(identifier="1", price=40000):
    return {"id": identifier, "title": "Legion", "price": price,
            "url": "https://www.avito.ru/kazan/noutbuki/example_1",
            "flags": {"notify_ready": True, "condition": "unknown", "warnings": ["проверить цену"]},
            "card": {"parsed": True, "reviewed_price": price,
                     "description": "Текст продавца", "parameters": "RTX 3050"}}


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(notify, "config", lambda: {"BOT_TOKEN": "1:test",
                        "TELEGRAM_CHAT_ID": "123", "TELEGRAM_ENABLED": "1"})


def test_delivery_deduplicates_and_sends_price_changes(tmp_path, configured):
    sender = Mock()
    report = {"candidates": [candidate()]}
    assert notify.deliver(report, tmp_path, sender)["sent"] == 1
    assert notify.deliver(report, tmp_path, sender)["sent"] == 0
    report["candidates"] = [candidate(price=35000)]
    assert notify.deliver(report, tmp_path, sender)["sent"] == 1
    assert "Ранее: 40000" in sender.call_args[0][0]
    assert "проверить цену" in sender.call_args[0][0]


def test_unchecked_and_stale_cards_are_not_sent(tmp_path, configured):
    item = candidate()
    item["card"]["reviewed_price"] = 50000
    sender = Mock()
    assert notify.deliver({"candidates": [item]}, tmp_path, sender)["sent"] == 0
    item["card"] = None
    assert notify.deliver({"candidates": [item]}, tmp_path, sender)["sent"] == 0
    sender.assert_not_called()


def test_failed_delivery_does_not_mark_sent_or_leak_message(tmp_path, configured):
    sender = Mock(side_effect=ValueError("sensitive response"))
    outcome = notify.deliver({"candidates": [candidate()]}, tmp_path, sender)
    assert outcome == {"status": "error", "error_type": "ValueError", "sent": 0}
    assert not (tmp_path / "telegram_sent.json").exists()


def test_description_risks():
    warnings = description_warnings("Цены от 15000 руб. Характеристики Авито неверные, читайте описание.")
    assert len(warnings) == 2
    assert description_warnings("Видеокарта GTX 1650", "RTX 3050")
    assert not description_warnings("Ноутбук MSI. Цена 42000 рублей. RTX 3050", "RTX 3050")


def test_review_rotation_and_price_priority():
    item = {"id": 1, "title": "Legion RTX 3050", "urlPath": "/kazan/noutbuki/test_1",
            "priceDetailed": {"value": 40000}}
    items = [item, {**item, "id": 2}]
    seen = {"1": {"card": {"parsed": True, "reviewed_price": 40000, "checked_at": "2026-09-20"}}}
    assert choose_details(items, seen, 1)[0]["id"] == 2


def test_telegram_uses_explicit_proxy_and_plain_text(monkeypatch):
    session = Mock()
    session.__enter__ = Mock(return_value=session)
    session.__exit__ = Mock(return_value=False)
    session.post.return_value.status_code = 200
    session.post.return_value.json.return_value = {"ok": True}
    monkeypatch.setattr(notify.requests, "Session", lambda: session)
    notify.send("test", {"BOT_TOKEN": "1:test", "TELEGRAM_CHAT_ID": "123",
                         "TELEGRAM_PROXY": "http://127.0.0.1:18080"})
    kwargs = session.post.call_args.kwargs
    assert session.trust_env is False
    assert kwargs["proxies"]["https"] == "http://127.0.0.1:18080"
    assert kwargs["allow_redirects"] is False
    assert "parse_mode" not in kwargs["json"]
