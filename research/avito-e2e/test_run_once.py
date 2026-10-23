import json
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import run_once as runner


@pytest.fixture(autouse=True)
def reset():
    runner.BoundedSession.events = []
    runner.BoundedSession.listings = []
    runner.BoundedSession.pages = []
    runner.BoundedSession.deadline = time.monotonic() + 30


def test_destination_and_budget_prevent_network():
    with runner.BoundedSession() as session, patch.object(runner.OriginalSession, "request") as call:
        for url in ("https://example.com", "https://avito.ru.evil.com", "http://avito.ru",
                    "https://avito.ru:444", "https://user:password@avito.ru"):
            with pytest.raises(RuntimeError):
                session.get(url)
        session.events.extend({} for _ in range(runner.REQUEST_LIMIT))
        with pytest.raises(RuntimeError):
            session.get("https://www.avito.ru/kazan/noutbuki")
        call.assert_not_called()


def test_full_upstream_one_page_without_live_network():
    runner.configure()
    items = [{"id": 123, "title": "Fixture", "urlPath": "/kazan/example_123",
              "priceDetailed": {"value": 100}, "private_field": "omit"}]
    def reply(session, method, url, **kwargs):
        assert kwargs["proxy"] == runner.PROXY
        assert kwargs["allow_redirects"] is False
        assert kwargs["timeout"] <= 10
        content = (json.dumps({"catalog": {"items": items}}).encode()
                   if "/web/1/js/items?" in url else b"<html></html>")
        return SimpleNamespace(status_code=200, headers={}, url=url, content=b"", text=content.decode(),
                               iter_content=lambda: iter((content,)),
                               json=lambda: json.loads(content))
    with patch.object(runner.OriginalSession, "request", reply), patch.object(
            runner.requests, "Session", runner.BoundedSession):
        flow = runner.upstream.run()
    assert len(flow.page_requests) == 1
    assert len(runner.BoundedSession.events) == 2
    assert len(runner.BoundedSession.listings) == 1
    assert "private_field" not in runner.BoundedSession.listings[0]


def test_classifier_does_not_assume_condition_or_city():
    from filters import classify, parse_card
    item = {"title": "Lenovo Legion RTX 3050", "urlPath": "/kazan/noutbuki/example_1",
            "priceDetailed": {"value": 40000}}
    assert classify(item)["candidate"]
    assert classify(item)["condition"] == "unknown"
    card = parse_card('<h1>Legion</h1><div data-marker="item-view/item-params">Состояние: Б/у Видеокарта: RTX 3050</div>')
    assert card["parsed"]
    assert classify(item, card=card)["condition"] == "used"
    assert not classify(item, card={"parameters": "Состояние: Новое"})["candidate"]
    assert not classify({**item, "urlPath": "/moskva/noutbuki/example_1"})["candidate"]
    assert not classify({**item, "title": "Legion RTX 3050 на запчасти"})["candidate"]


def test_plain_403_stops_without_retry():
    runner.configure()
    def reply(session, method, url, **kwargs):
        return SimpleNamespace(status_code=403, headers={}, url=url, content=b"",
                               text="Forbidden", json=lambda: {},
                               iter_content=lambda: iter((b"Forbidden",)))
    with patch.object(runner.OriginalSession, "request", reply), patch.object(
            runner.requests, "Session", runner.BoundedSession):
        with pytest.raises(RuntimeError):
            runner.upstream.run()
    assert len(runner.BoundedSession.events) == 1
