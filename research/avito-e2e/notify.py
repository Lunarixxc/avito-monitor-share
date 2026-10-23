"""One-way Telegram notifications. Secrets are never printed."""
import argparse
import json
import os
from pathlib import Path
import re

import requests

ROOT = Path(__file__).resolve().parent


def config():
    values = {"TELEGRAM_ENABLED": "0", "TELEGRAM_PROXY": "http://127.0.0.1:18080"}
    path = ROOT / ".env"
    allowed = {"BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_ENABLED", "TELEGRAM_PROXY"}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, separator, value = line.partition("=")
            if separator and key.strip() in allowed:
                values[key.strip()] = value.strip().strip('"\'')
    for key in allowed:
        if key in os.environ:
            values[key] = os.environ[key]
    return values


def ready(values):
    return (values.get("TELEGRAM_ENABLED") == "1"
            and bool(re.fullmatch(r"\d+:[A-Za-z0-9_-]+", values.get("BOT_TOKEN", "")))
            and bool(re.fullmatch(r"-?\d+", values.get("TELEGRAM_CHAT_ID", ""))))


def render(item, previous=None):
    from filters import specifications, seller_info
    label = "Новый кандидат" if previous is None else "Изменилась цена"
    card = {**(item["card"] or {}), "id": item.get("id", "")}
    specs = specifications(card) if card.get("parameters") or card.get("description") else (item["flags"].get("specs") or specifications(card))
    vram = specifications(card)["vram_gb"]
    gpu = f"{specs['gpu']} {vram} GB" if vram else f"{specs['gpu']} — объём видеопамяти не указан"
    seller = item["flags"].get("seller") or seller_info(card)
    lines = [label + " — " + str(item["title"])[:300], f"Цена объявления: {item['price']} ₽"]
    if previous is not None:
        lines.append(f"Ранее: {previous} ₽")
    lines.append("Характеристики (заявлены продавцом):\n"
                 f"Видеокарта: {gpu}\n"
                 f"Оперативная память: {specs['ram']}\n"
                 f"Процессор: {specs['cpu']}\n"
                 f"Накопитель: {specs['storage']}")
    lines.append("Продавец: " + seller["label"])
    summary = seller_summary(card)
    if summary:
        lines.append(summary)
    lines.append("Город: Казань")
    if item.get("analysis"):
        lines.append(short_assessment(item["analysis"]))
    warnings = item["flags"].get("warnings", [])
    if warnings:
        lines.append("Важно: " + "; ".join(warnings[:2]))
    if card.get("description"):
        description = card["description"]
        if len(description) > 700:
            description = description[:697].rsplit(" ", 1)[0] + "…"
        lines.append("Описание продавца:\n" + description)
    return "\n\n".join(lines)[:3900 - len(item["url"]) - 2] + "\n\n" + item["url"]


def seller_summary(card):
    profile = (card.get("seller_text") or "").split("\n", 1)[0]
    reviews = re.search(r"\b\d+\s+отзыв(?:а|ов)?\b", profile, re.I)
    if reviews:
        profile = profile[:reviews.end()]
    else:
        profile = re.split(r"Частное лицо|Компания|Профиль|Подписаться|Данные подтверждены|timing", profile, maxsplit=1)[0]
    return profile.strip(" ·")[:180]


def short_assessment(a):
    lines = ["Оценка: " + a["verdict"]]
    lines.append(price_comparison(a))
    # Keep concrete serious risks, omit generic checklists and missing-data notices.
    for risk in a.get("risk_signals", []):
        if risk["code"] in ("repair", "defect"):
            lines.append("Важно: " + risk["label"] + " — «" + risk["evidence"] + "»")
    return "\n".join(lines)


def price_comparison(a):
    """Concise price context; a new-laptop discount is not a used-market verdict."""
    new = a.get("new_comparison") or {}
    if new.get("available"):
        saving = new["saving_pct"]
        label = "дешевле" if saving >= 0 else "дороже"
        scope = "тот же GPU/класс CPU/ОЗУ" if new["match"] == "segment" else "только GPU, приблизительно"
        caveats = []
        if new.get("stale"):
            caveats.append("таблица старше 45 дней")
        if new["row"].get("issues") or new["row"].get("rare"):
            caveats.append("ориентир ограничен")
        if caveats:
            scope += "; " + "; ".join(caveats)
        source_date = new["source_date"]
        source_date = source_date[8:10] + "." + source_date[5:7]
        percent = f"{abs(saving):g}".replace(".", ",")
        new_line = f"К новым по вашей таблице от {source_date}: {label} на {percent}% ({scope})."
    else:
        new_line = "К новым по вашей таблице: подходящего ориентира нет."
    used = a.get("used_comparison") or {}
    if used.get("sufficient"):
        difference = used["difference_pct"]
        percent = f"{abs(difference):g}".replace(".", ",")
        if difference == 0:
            used_line = f"Похожие б/у: на уровне медианы ({used['n']} объявлений)."
        else:
            label = "дешевле" if difference < 0 else "дороже"
            used_line = (f"Похожие б/у: {label} медианы на {percent}% "
                         f"({used['n']} объявлений).")
    else:
        used_line = f"Похожие б/у: {used.get('n', 0)}/{used.get('min_required', 5)} точных аналогов; оценки рынка пока нет."
    return new_line + "\n" + used_line


def linked_text(text):
    """Replace standalone Avito URLs with labels and Telegram text_link entities."""
    result, entities, cursor = "", [], 0
    for match in re.finditer(r"^https?://(?:www\.)?avito\.ru/[^\s]+$", text, re.M):
        result += text[cursor:match.start()]
        # Telegram counts UTF-16 code units, including two units for emoji.
        entities.append({"type": "text_link", "offset": len(result.encode("utf-16-le")) // 2,
                         "length": len("ссылка"), "url": match.group()})
        result += "ссылка"
        cursor = match.end()
    return result + text[cursor:], entities


def send(text, values, reply_markup=None):
    # Use an explicit VPN proxy only for Telegram. Do not inherit proxy env.
    with requests.Session() as session:
        session.trust_env = False
        proxy = values.get("TELEGRAM_PROXY")
        if not proxy:
            raise ValueError("telegram_proxy_missing")
        text, entities = linked_text(text)
        payload = {"chat_id": values["TELEGRAM_CHAT_ID"], "text": text,
                   "link_preview_options": {"is_disabled": True}}
        if entities:
            payload["entities"] = entities
        if reply_markup:
            payload["reply_markup"] = reply_markup
        response = session.post(
            "https://api.telegram.org/bot" + values["BOT_TOKEN"] + "/sendMessage",
            json=payload,
            proxies={"http": proxy, "https": proxy}, timeout=15,
            allow_redirects=False)
        body = response.json()
        if response.status_code != 200 or body.get("ok") is not True:
            raise ValueError("telegram_rejected")


def deliver(report, state_dir, sender=send):
    values = config()
    if not ready(values):
        return {"status": "not_configured", "sent": 0}
    path = state_dir / "telegram_sent.json"
    sent = json.loads(path.read_text()) if path.exists() else {}
    count = 0
    for item in report["candidates"]:
        card = item.get("card") or {}
        # No unchecked or stale-price candidates in Telegram.
        if (not item["flags"].get("notify_ready", False)
                or not card.get("parsed") or card.get("reviewed_price") != item["price"]):
            continue
        key = str(item["id"])
        previous = sent.get(key)
        if previous == item["price"]:
            continue
        try:
            sender(render(item, previous), values)
        except Exception as exc:
            # Requests exceptions embed the bot token in the URL; log class only.
            return {"status": "error", "error_type": type(exc).__name__, "sent": count}
        sent[key] = item["price"]
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(sent))
        temporary.replace(path)
        count += 1
        if count >= 8:
            break
    return {"status": "ok", "sent": count}


if __name__ == "__main__":
    argparse.ArgumentParser(description="Offline Telegram configuration check").parse_args()
    values = config()
    print(json.dumps({"enabled": values.get("TELEGRAM_ENABLED") == "1",
                      "token_present": bool(values.get("BOT_TOKEN")),
                      "chat_id_present": bool(values.get("TELEGRAM_CHAT_ID")),
                      "ready": bool(ready(values))}))
