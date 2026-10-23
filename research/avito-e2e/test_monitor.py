import copy

import pytest

from monitor import reconcile


def snapshot(price=40000, status="success"):
    return {"started": "2026-09-20T15:00:00Z", "status": status,
            "listings": [{"id": 1, "title": "Lenovo Legion RTX 3050",
                          "urlPath": "/kazan/noutbuki/test_1", "priceDetailed": {"value": price}}]}


def test_baseline_dedup_price_change_and_no_false_removal():
    state, report = reconcile({}, snapshot())
    assert report["baseline"] and not report["changes"]
    state2, report = reconcile(state, snapshot())
    assert not report["changes"]
    state3, report = reconcile(state2, snapshot(35000))
    assert report["changes"][0]["type"] == "price_down"
    assert report["changes"][0]["previous_price"] == 40000
    empty = {**snapshot(), "listings": []}
    state4, report = reconcile(state3, empty)
    assert "1" in state4["seen"] and not report["changes"]


def test_failure_cannot_replace_good_history():
    state, _ = reconcile({}, snapshot())
    original = copy.deepcopy(state)
    with pytest.raises(ValueError):
        reconcile(state, snapshot(status="failed"))
    assert state == original


def test_new_candidate_and_bulk_exclusion():
    state, _ = reconcile({}, snapshot())
    result = snapshot()
    result["listings"][0]["id"] = 2
    _, report = reconcile(state, result)
    assert report["changes"][0]["type"] == "new"
    result["listings"][0]["title"] = "Игровые ноутбуки RTX 3050 в ассортименте"
    _, report = reconcile(state, result)
    assert all(x["id"] != "2" for x in report["candidates"])
    assert not report["changes"]
