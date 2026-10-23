"""Offline, deterministic assessment. No network or model dependencies."""
import argparse
import datetime as dt
import hashlib
import json
import re
from pathlib import Path
from statistics import median

from filters import classify, gpu_text, specifications

ROOT = Path(__file__).resolve().parent
REFERENCE = ROOT / "data" / "new_prices_2026-09.json"
RULE_VERSION = "1.0"
WINDOW_DAYS = 30
MIN_PEERS = 5


def day(value):
    return dt.date.fromisoformat(value[:10])


def load_reference(path=REFERENCE):
    data = json.loads(path.read_text())
    if data["schema_version"] != 1:
        raise ValueError("unsupported_reference_schema")
    day(data["as_of"])
    keys = set()
    for row in data["segments"] + data["gpu_only"]:
        key = (row["gpu"], row.get("cpu_tier"), row.get("ram_gb"))
        if key in keys or type(row["mean_rub"]) not in (int, float) or row["mean_rub"] <= 0:
            raise ValueError("invalid_or_duplicate_reference_row")
        keys.add(key)
    data["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return data


def facts(item):
    card = item.get("card") or {}
    specs = specifications({**card, "id": item.get("id", card.get("id", ""))})
    cpu_text = specs["cpu"].lower()
    cpu = re.search(r"\bi[3579][- ]?\d{4,5}[a-z]{1,2}\b|"
                    r"ryzen\s+(?:ai\s+)?[3579]\s+\d{3,4}[a-z]{0,2}\b|"
                    r"(?:core\s+)?(?:ultra\s+)?[579]\s+\d{3}[a-z]{1,2}\b", cpu_text)
    cpu = re.sub(r"[\s-]+", "", cpu.group()) if cpu else None
    tier = None
    # Only the supplied families/models are mapped. Do not call old i7s 'top'.
    budget = ("7535hs", "7235hs", "6600h", "5600h", "7640hs", "12450h",
              "13420h", "12500h", "13500h", "210h", "120h")
    performance = ("7745hx", "7840hs", "8845hs", "8745h", "7735hs", "9955hx",
                   "12700h", "13620h", "13650hx", "13700hx", "14650hx", "14900hx")
    if cpu and any(cpu.endswith(x) for x in budget):
        tier = "budget"
    elif cpu and (any(cpu.endswith(x) for x in performance) or "ultra7" in cpu or "ultra9" in cpu):
        tier = "performance"
    gpus = set(re.findall(r"RTX\s*(\d{4}(?:\s*TI)?)\b", gpu_text(specs["gpu"]).upper()))
    gpu = "RTX " + re.sub(r"\s+", "", next(iter(gpus))) if len(gpus) == 1 else None
    ram = re.fullmatch(r"(\d+) ГБ", specs["ram"])
    params = card.get("parameters", "").replace("Объём", "Объем")
    vram = re.search(r"Объем видеопамяти, ГБ\s*:\s*(\d+)\b", params, re.I)
    condition = re.search(r"\bСостояние\s*:\s*(Б/у|Отличное|Хорошее|Удовлетворительное|Новое)\b", params, re.I)
    return {"gpu": gpu, "cpu": cpu, "cpu_tier": tier,
            "vram_gb": specs.get("vram_gb"),
            "condition_detail": condition[1].lower() if condition else None,
            "ram_gb": int(ram[1]) if ram else None, "storage": specs["storage"],
            "seller_type": item.get("flags", {}).get("seller", {}).get("type", "unknown"),
            "condition": item.get("flags", {}).get("condition", "unknown")}


def new_comparison(f, price, reference, as_of):
    row = next((r for r in reference["segments"] if
                (r["gpu"], r["cpu_tier"], r["ram_gb"]) ==
                (f["gpu"], f["cpu_tier"], f["ram_gb"])), None)
    match = "segment" if row else "gpu_only"
    if row is None:
        row = next((r for r in reference["gpu_only"] if r["gpu"] == f["gpu"]), None)
    age = (day(as_of) - day(reference["as_of"])).days
    if row is None or age < 0:
        return {"available": False, "reason": "нет подходящей строки на дату оценки"}
    return {"available": True, "match": match, "source_id": reference["id"],
            "source_date": reference["as_of"], "verification": reference["verification"],
            "age_days": age, "stale": age > 45, "row": row,
            "saving_rub": row["mean_rub"] - price,
            "saving_pct": round(100 * (row["mean_rub"] - price) / row["mean_rub"], 1)}


def risk_signals(card):
    """Evidence snippets, never an assertion that a device was inspected."""
    description = card.get("description", "")
    patterns = [
        ("repair", "История ремонта требует уточнения", r"после ремонта|ремонтировал\w*|менял\w* (?:чип|видеочип|плату)|реболл\w*|прогрев\w*"),
        ("defect", "Возможен существенный дефект", r"не работа(?:ет|ют)|не функционир\w*|трещин\w*|не включается|перегрева\w*|артефакт\w*|разбит\w*|полосы на экране|не держит заряд"),
        ("peripheral", "Некорректно работает тачпад", r"некорректно работа(?:ет|ют) тачпад"),
        ("cosmetic", "Указаны внешние недостатки", r"царапин\w*|пот[её]ртост\w*|засвет\w*|пятн\w*"),
    ]
    signals = []
    for code, label, pattern in patterns:
        for m in re.finditer(pattern, description, re.I):
            before = description[max(0, m.start()-20):m.start()].lower()
            after = description[m.end():m.end()+20].lower()
            if re.search(r"(?:без|нет|не было|не)\s*$", before) or re.match(r"\s*(?:нет|не было|отсутствуют)\b", after):
                continue
            signals.append({"code": code, "label": label,
                            "evidence": description[max(0, m.start()-30):m.end()+65]})
            break
    if card.get("parameters"):
        for signal in risk_signals({"description": card["parameters"]}):
            if not any(x["code"] == signal["code"] for x in signals):
                signals.append({**signal, "source": "parameters"})
    for risk in (card.get("semantic_analysis") or {}).get("payload", {}).get("risks", []):
        if risk["code"] in ("repair", "defect") and not any(x["code"] == risk["code"] for x in signals):
            signals.append({"code": risk["code"], "label": "Требует проверки по разбору DeepSeek",
                            "evidence": risk["evidence"], "source": "deepseek"})
    return signals


def usable(item, as_of):
    card = item.get("card") or {}
    try:
        age = (day(as_of) - day(card.get("checked_at", ""))).days
        seen_age = (day(as_of) - day(item.get("last_seen", ""))).days
    except (ValueError, TypeError):
        return False
    return (item.get("flags", {}).get("notify_ready", False)
            and card.get("reviewed_price") == item.get("price")
            and 0 <= age <= WINDOW_DAYS and 0 <= seen_age <= WINDOW_DAYS
            and not any(r["code"] in ("repair", "defect") for r in risk_signals(card)))


def used_comparison(item, peers, as_of):
    f = facts(item)
    keys = ("gpu", "vram_gb", "cpu", "ram_gb", "storage", "seller_type", "condition_detail")
    matches = {}
    complete = (all(f[k] for k in ("gpu", "vram_gb", "cpu", "ram_gb", "condition_detail"))
                and "не указан" not in f["storage"] and f["seller_type"] != "unknown")
    if complete:
        for peer in peers:
            identifier = str(peer["id"])
            if identifier == str(item["id"]) or not usable(peer, as_of):
                continue
            other = facts(peer)
            if any(f[k] != other[k] for k in keys):
                continue
            if identifier not in matches or peer["last_seen"] > matches[identifier]["last_seen"]:
                matches[identifier] = peer
    prices = [p["price"] for p in matches.values()]
    mid = median(prices) if prices else None
    return {"n": len(prices), "min_required": MIN_PEERS, "window_days": WINDOW_DAYS,
            "sufficient": len(prices) >= MIN_PEERS, "median_rub": mid,
            "range_rub": [min(prices), max(prices)] if prices else None,
            "difference_pct": round(100 * (item["price"] - mid) / mid, 1) if mid else None,
            "peer_ids": sorted(matches), "match_fields": list(keys),
            "scope": "Ограниченная выборка Авито; преимущественно до 60 000 ₽. Цены предложений, не сделок."}


def assess(item, peers, reference, as_of, previous_price=None):
    f = facts(item)
    card = item.get("card") or {}
    signals = risk_signals(card)
    new = new_comparison(f, item["price"], reference, as_of)
    used = used_comparison(item, peers, as_of)
    flags = item.get("flags", {})
    reasons = list(flags.get("rejection_reasons", []))
    positives = []
    if f["gpu"] == "RTX 4050":
        positives.append("Предпочтительная видеокарта RTX 4050")
    if f["ram_gb"] and f["ram_gb"] >= 16:
        positives.append("Оперативная память не менее 16 ГБ")
    unknowns = ["TGP, охлаждение и реальная исправность не проверены",
                "Нет подтверждённых тестов батареи и накопителя"]
    if f["cpu"] is None:
        unknowns.append("Модель процессора не распознана")
    if f["ram_gb"] is None:
        unknowns.append("Объём ОЗУ неизвестен")
    elif f["ram_gb"] < 16:
        reasons.append("ОЗУ меньше желательных 16 ГБ; стоимость расширения не учтена")
    if f["condition"] == "unknown":
        unknowns.append("Состояние б/у не подтверждено отдельным полем")
    verdict = "Недостаточно данных для оценки выгодности"
    confidence = "низкая"
    if not flags.get("candidate"):
        verdict = "Пропустить: не проходит фильтры"
    elif not usable(item, as_of):
        verdict = "Требуется проверка карточки или рисков"
    elif used["sufficient"]:
        difference = used["difference_pct"]
        if difference < -35:
            verdict = "Подозрительно низкая цена: нужна проверка"
        elif difference <= -10:
            verdict = "Стоит проверить: дешевле найденных аналогов"
        elif difference >= 10:
            verdict = "Дороговато относительно найденных аналогов"
        else:
            verdict = "Обычная цена среди найденных аналогов"
        confidence = "средняя"
    if any(s["code"] in ("repair", "defect") for s in signals):
        verdict = "Сначала уточнить ремонт или дефект" if flags.get("candidate") else verdict
        confidence = "низкая"
    checks = ["Запросить тест под нагрузкой и отчёт износа батареи",
              "Уточнить ремонт, проверить экран, SSD и комплект при встрече"]
    if signals:
        checks.insert(0, "Уточнить у продавца отмеченные в описании недостатки")
    context = {"facts": f, "price_rub": item["price"], "card": card,
               "new": new, "used": used, "as_of": as_of, "reference_hash": reference["sha256"]}
    return {"schema_version": 1, "rule_version": RULE_VERSION, "as_of": as_of,
            "input_sha256": hashlib.sha256(json.dumps(context, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
            "facts": f, "verdict": verdict, "confidence": confidence,
            "new_comparison": new, "used_comparison": used, "risk_signals": signals,
            "reasons": reasons, "positives": positives, "unknowns": unknowns, "checks": checks,
            "price_change_rub": item["price"] - previous_price if previous_price is not None else None,
            "semantic_analysis": card.get("semantic_analysis")}


def render_analysis(a):
    lines = ["Оценка: " + a["verdict"], "Уверенность: " + a["confidence"]]
    new = a["new_comparison"]
    if new["available"]:
        row = new["row"]
        qualifier = "тот же сегмент CPU/ОЗУ/GPU" if new["match"] == "segment" else "только GPU; CPU/ОЗУ могут отличаться"
        lines.append(f"Новые: ориентир {row['mean_rub']:,} ₽ ({qualifier}), таблица пользователя {new['source_date']}.".replace(",", " "))
        label = "Дешевле ориентира" if new["saving_rub"] >= 0 else "Дороже ориентира"
        lines.append(f"{label}: {abs(new['saving_rub']):,} ₽ ({abs(new['saving_pct'])}%).".replace(",", " "))
        if new["stale"]:
            lines.append("Таблица старше 45 дней: цена требует обновления.")
        if row.get("issues") or row.get("rare"):
            lines.append("Ориентир ненадёжен: редкая выборка или неоднозначности исходной строки.")
        lines.append("Приблизительная непроверенная таблица; TGP, модель и SSD могут отличаться.")
    else:
        lines.append("Новые: подходящего ориентира нет.")
    used = a["used_comparison"]
    lines.append(f"Б/у: {used['n']} аналогов за {used['window_days']} дней, без этого объявления.")
    if used["sufficient"]:
        lines.append(f"Медиана {used['median_rub']:g} ₽; диапазон {used['range_rub'][0]}–{used['range_rub'][1]} ₽; отклонение {used['difference_pct']:+g}%.")
    else:
        lines.append(f"Для вывода по б/у нужно минимум {used['min_required']} аналогов.")
    lines.append("Охват ограничен бюджетом и страницами; это цены предложений.")
    if a.get("price_change_rub"):
        lines.append(f"Изменение с прошлого наблюдения: {a['price_change_rub']:+g} ₽.")
    if a["positives"]:
        lines.append("Плюсы: " + "; ".join(a["positives"]))
    for s in a["risk_signals"][:2]:
        lines.append(s["label"] + ": «" + s["evidence"] + "»")
    if a["reasons"]:
        lines.append("Ограничения: " + "; ".join(a["reasons"][:2]))
    lines.append("Не проверено: " + a["unknowns"][0])
    lines.append("Перед покупкой: " + "; ".join(a["checks"][:2]))
    return "\n".join(lines)


def attach_assessments(state, report, previous=None, reference=None):
    reference = reference or load_reference()
    for item in report["candidates"]:
        old = (previous or {}).get("seen", {}).get(str(item["id"]), {})
        item["analysis"] = assess(item, state["seen"].values(), reference, report["snapshot"], old.get("price"))


def main():
    parser = argparse.ArgumentParser(description="Offline assessment; never fetches or sends messages")
    parser.add_argument("--state", type=Path, default=ROOT / "results/monitor/state.json")
    parser.add_argument("--output", type=Path, default=ROOT / "results/monitor/assessment.json")
    parser.add_argument("--as-of", default=dt.datetime.now(dt.timezone.utc).date().isoformat())
    args = parser.parse_args()
    state = json.loads(args.state.read_text())
    # Reclassify old snapshots without rewriting the operational state or history.
    for entry in state["seen"].values():
        entry.pop("analysis", None)
        entry["flags"] = classify({"title": entry["title"], "urlPath": entry["url"],
                                   "priceDetailed": {"value": entry["price"]}, "id": entry["id"]}, card=entry.get("card"))
    report = {"snapshot": args.as_of, "candidates": [e for e in state["seen"].values()
                                                      if e["flags"]["candidate"]]}
    attach_assessments(state, report)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    from notify import render
    cards = [render(e) for e in report["candidates"] if (e.get("card") or {}).get("parsed")]
    args.output.with_suffix(".md").write_text("# Оценка сохранённых объявлений\n\nДата оценки: " + args.as_of
                                             + "\n\n" + "\n\n---\n\n".join(cards) + "\n")
    print(json.dumps({"assessed": len(report["candidates"]), "read_cards": len(cards),
                      "output": str(args.output), "network": False}))


if __name__ == "__main__":
    main()
