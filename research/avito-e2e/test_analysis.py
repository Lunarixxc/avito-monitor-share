import copy
import json

import pytest

from analysis import (assess, attach_assessments, facts, load_reference,
                      main, new_comparison, risk_signals, used_comparison)
from filters import classify
from notify import render


def candidate(identifier=1, price=42000, cpu="Intel Core i5-12500H", ram=16, gpu="RTX 3050"):
    card = {"parsed": True, "checked_at": "2026-09-20T12:00:00Z", "reviewed_price": price,
            "parameters": f"Состояние: Б/у Процессор: {cpu} Оперативная память, ГБ: {ram} "
                          f"Конфигурация накопителей: SSD Объем накопителей, ГБ: 512 Видеокарта: {gpu} Объем видеопамяти, ГБ: 4",
            "seller_text": "Иван Частное лицо", "description": "Без ремонта, царапин нет"}
    item = {"title": "MSI RTX 3050", "urlPath": "/kazan/noutbuki/test_1", "priceDetailed": {"value": price}}
    return {"id": str(identifier), "title": item["title"], "url": "https://www.avito.ru"+item["urlPath"],
            "price": price, "card": card, "flags": classify(item, card=card),
            "first_seen": "2026-09-20T12:00:00Z", "last_seen": "2026-09-20T12:00:00Z"}


def test_msi_reference_arithmetic_and_small_used_sample():
    item = candidate()
    a = assess(item, [item], load_reference(), "2026-09-20")
    assert a["new_comparison"]["row"]["mean_rub"] == 80000
    assert a["new_comparison"]["saving_rub"] == 38000
    assert a["new_comparison"]["saving_pct"] == 47.5
    assert a["used_comparison"]["n"] == 0
    assert a["confidence"] == "низкая"
    assert "Недостаточно" in a["verdict"]
    assert a["semantic_analysis"] is None


@pytest.mark.parametrize("cpu,ram,gpu,match", [
    ("Intel Core i5-12500H", 24, "RTX 3050", "gpu_only"),
    ("Intel Core i7-9750H", 16, "RTX 3050", "gpu_only"),
    ("Intel Core i7-13620H", 16, "RTX 4060", "segment"),
    ("AMD Ryzen 7 260", 16, "RTX 5050", "gpu_only"),
])
def test_reference_does_not_invent_exact_matches(cpu, ram, gpu, match):
    item = candidate(cpu=cpu, ram=ram, gpu=gpu)
    assert new_comparison(facts(item), 42000, load_reference(), "2026-09-20")["match"] == match


def test_ti_not_collapsed_and_stale_or_future_reference():
    ref = load_reference()
    assert not new_comparison(facts(candidate(gpu="RTX 3050 Ti")), 42000, ref, "2026-09-20")["available"]
    assert new_comparison(facts(candidate()), 42000, ref, "2026-12-20")["stale"]
    assert not new_comparison(facts(candidate()), 42000, ref, "2026-09-19")["available"]
    row = next(r for r in ref["segments"] if r["gpu"] == "RTX 5060" and r["cpu_tier"] == "performance")
    assert row["issues"]


def test_used_dedup_self_exclusion_matching_and_age():
    item = candidate()
    peers = [candidate(i, 50000) for i in range(2, 7)]
    stale = candidate(20)
    stale["card"]["checked_at"] = "2026-07-01"
    changed = candidate(21)
    changed["card"]["reviewed_price"] = 41000
    shop = candidate(22)
    shop["flags"]["seller"]["type"] = "shop"
    broken = candidate(23)
    broken["card"]["description"] = "После ремонта платы"
    sample = peers + peers + [item, stale, changed, shop, broken, candidate(24, ram=8)]
    result = used_comparison(item, sample, "2026-09-20")
    assert result["n"] == 5 and result["median_rub"] == 50000
    assert result["difference_pct"] == -16
    a = assess(item, sample, load_reference(), "2026-09-20")
    assert a["verdict"].startswith("Стоит проверить")


@pytest.mark.parametrize("price,verdict", [(50000, "Обычная"), (56000, "Дороговато"), (30000, "Пропустить")])
def test_price_verdict_boundaries(price, verdict):
    a = assess(candidate(price=price), [candidate(i, 50000) for i in range(2, 7)], load_reference(), "2026-09-20")
    assert a["verdict"].startswith(verdict)


def test_risks_negations_and_cheap_damaged_not_recommended():
    assert not risk_signals({"description": "Без ремонта, не перегревается, царапин нет, артефактов нет"})
    item = candidate(price=35000)
    item["card"]["description"] = "После ремонта. Есть царапины на корпусе"
    a = assess(item, [candidate(i, 50000) for i in range(2, 7)], load_reference(), "2026-09-20")
    assert a["verdict"].startswith("Сначала уточнить")
    assert a["risk_signals"][0]["evidence"] in item["card"]["description"]
    assert a["confidence"] == "низкая"


def test_unread_and_stale_candidate_cannot_get_positive_verdict():
    item = candidate()
    item["card"]["checked_at"] = "2026-06-01"
    a = assess(item, [candidate(i, 50000) for i in range(2, 7)], load_reference(), "2026-09-20")
    assert a["verdict"].startswith("Требуется проверка")


def test_report_attachment_render_and_no_overwrite_of_filters():
    item = candidate()
    flags = copy.deepcopy(item["flags"])
    report = {"snapshot": "2026-09-20", "candidates": [item]}
    state = {"seen": {"1": item}}
    attach_assessments(state, report)
    assert item["flags"] == flags
    assert state["seen"]["1"]["analysis"] == report["candidates"][0]["analysis"]
    message = render(item)
    assert "Оценка:" in message and "47,5%" in message
    assert message.endswith(item["url"]) and len(message) <= 3900


def test_offline_cli_preserves_state_and_creates_review(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"seen": {"1": candidate()}}))
    before = path.read_bytes()
    output = tmp_path / "review.json"
    monkeypatch.setattr("sys.argv", ["analysis.py", "--state", str(path), "--output", str(output), "--as-of", "2026-09-20"])
    main()
    assert path.read_bytes() == before
    assert "Оценка:" in output.with_suffix(".md").read_text()
    assert json.loads(output.read_text())["candidates"][0]["analysis"]["schema_version"] == 1


def test_vram_and_condition_must_match():
    item = candidate()
    six_gb = candidate(2)
    six_gb["card"]["parameters"] = six_gb["card"]["parameters"].replace("видеопамяти, ГБ: 4", "видеопамяти, ГБ: 6")
    other_condition = candidate(3)
    other_condition["card"]["parameters"] = other_condition["card"]["parameters"].replace("Б/у", "Хорошее")
    assert used_comparison(item, [six_gb, other_condition], "2026-09-20")["n"] == 0


def test_duplicate_reference_rejected(tmp_path):
    data = load_reference()
    data["segments"].append(data["segments"][0])
    path = tmp_path / "reference.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="duplicate"):
        load_reference(path)


def test_monitor_import_persists_analysis_without_delivery(tmp_path, monkeypatch):
    import monitor
    item = candidate()
    snapshot = {"status": "success", "started": "2026-09-20T12:00:00Z",
                "listings": [{"id": 1, "title": item["title"], "urlPath": "/kazan/noutbuki/test_1",
                              "priceDetailed": {"value": 42000}}],
                "cards": [{"id": 1, **item["card"]}]}
    source = tmp_path / "summary.json"
    source.write_text(json.dumps(snapshot))
    state_dir = tmp_path / "state"
    monkeypatch.setattr(monitor, "STATE", state_dir)
    monkeypatch.setattr("sys.argv", ["monitor.py", "--import-result", str(source)])
    def unexpected(*args):
        raise AssertionError("offline import must not deliver")
    monkeypatch.setattr("notify.deliver", unexpected)
    assert monitor.main() == 0
    state = json.loads((state_dir / "state.json").read_text())
    assert state["seen"]["1"]["analysis"]["new_comparison"]["saving_rub"] == 38000
    assert "Оценка:" in (state_dir / "latest.md").read_text()
    history = json.loads((state_dir / "history.jsonl").read_text())
    assert history["candidates"][0]["analysis"]["rule_version"] == "1.0"


def test_concise_message_preserves_analysis_and_relevant_risks():
    item = candidate()
    item["card"]["seller_text"] = "Пользователь 5,0 1 отзыв Частное лицо Подписаться Данные подтверждены timing\nПользователь"
    a = assess(item, [item], load_reference(), "2026-09-20")
    item["analysis"] = a
    original = copy.deepcopy(a)
    text = render(item)
    assert text.index("Цена объявления:") < text.index("Характеристики") < text.index("Продавец:")
    assert "Пользователь 5,0 1 отзыв\n" in text
    for noise in ("timing", "Подписаться", "минимум", "Уверенность:", "Б/у:", "TGP", "Перед покупкой:"):
        assert noise not in text
    assert a == original and a["used_comparison"]["min_required"] == 5
    a["risk_signals"] = [{"code": "repair", "label": "Ремонт", "evidence": "После ремонта платы"}]
    assert "После ремонта платы" in render(item)


def test_short_price_comparison_labels_reference_scope_and_missing_peers():
    from notify import price_comparison, short_assessment
    exact = assess(candidate(), [], load_reference(), "2026-09-20")
    assert "47,5% (тот же GPU/класс CPU/ОЗУ)" in short_assessment(exact)
    a = assess(candidate(ram=24), [], load_reference(), "2026-09-20")
    assert a["new_comparison"]["available"]
    assert "только GPU, приблизительно" in short_assessment(a)
    assert "0/5 точных аналогов" in short_assessment(a)
    missing = assess(candidate(gpu="RTX 3050 Ti"), [], load_reference(), "2026-09-20")
    assert "подходящего ориентира нет" in price_comparison(missing)
    exact["used_comparison"].update(sufficient=True, n=5, difference_pct=-16)
    assert "Похожие б/у: дешевле медианы на 16%" in price_comparison(exact)


def test_nonfunctional_keys_and_housing_cracks_survive_without_model():
    for description in ('не функционируют 4 клавиши F3, F4, 3, Е(у)', 'По корпусу имеется трещины'):
        assert any(x['code'] == 'defect' for x in risk_signals({'description': description}))
