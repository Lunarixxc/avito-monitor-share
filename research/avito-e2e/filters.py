"""Conservative candidate selection; unknown condition is never inferred as used."""
import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from feedback import identify, review

GAMING = re.compile(r"игров|\b(?:rtx|gtx)\s*\d{3,4}|\b(?:legion|nitro|tuf|rog|predator|omen|victus)\b", re.I)
PARTS = re.compile(r"запчаст|не(?:исправ|рабоч| включается)|разбор|ремонт ноутбук|куплю|скупк|аренда|без видеокарт", re.I)
BULK = re.compile(r"ноутбуки\b|ноутбуков\b|подбор|ноутбук\s*,\s*ультрабук|i[3579]\s*/\s*i[3579]", re.I)
MIN_PRICE = 35000

# Explicit supported families; model numbers are not a performance benchmark.
GPU_MODELS = {2070, 3050, 3060, 3070, 3080, 4050, 4060, 4070, 4080, 4090,
              5050, 5060, 5070, 5080, 5090}
LABELS = ("Производитель", "Модель", "Диагональ, дюйм", "Разрешение экрана",
          "Процессор", "Количество ядер процессора", "Оперативная память, ГБ",
          "Конфигурация накопителей", "Объем накопителей, ГБ", "Видеокарта",
          "Объем видеопамяти, ГБ", "Операционная система", "Состояние")


def gpu_text(value):
    return value.translate(str.maketrans({"Т": "T", "т": "t", "Х": "X", "х": "x"}))


def gpu_models(value):
    """Expand GPU lists like RTX 3050/3060; keep GTX and Ti distinct."""
    models = []
    for match in re.finditer(r"\b(RTX|GTX)[\s-]*(\d{3,4}(?:\s*TI)?(?:\s*[/,]\s*\d{3,4}(?:\s*TI)?)*)",
                             gpu_text(value).upper()):
        for number in re.findall(r"\d{3,4}(?:\s*TI)?", match[2]):
            model = match[1] + " " + re.sub(r"\s*(TI)$", r" \1", number)
            if model not in models:
                models.append(model)
    return models


def specifications(card, title=""):
    params = card.get("parameters", "").replace("Объём", "Объем")
    boundary = "|".join(re.escape(x) for x in LABELS)
    def field(label):
        match = re.search(re.escape(label) + r"\s*:\s*(.*?)(?=\s*(?:" + boundary
                          + r")\s*:|Все характеристики|\n|$)", params, re.I)
        return match.group(1).strip() if match else ""
    # Once a card is read, its actual contents must confirm the title's GPU.
    source = params + " " + card.get("description", "") if (params or card.get("description")) else title
    gpu = field("Видеокарта")
    if not gpu:
        gpu = " / ".join(gpu_models(source))
    storage, size = field("Конфигурация накопителей"), field("Объем накопителей, ГБ")
    vram = re.fullmatch(r"(\d+)\s*(?:ГБ|GB)?", field("Объем видеопамяти, ГБ"), re.I)
    specs = {"gpu": gpu or "не указана", "vram_gb": int(vram[1]) if vram and int(vram[1]) > 0 else None,
            "ram": (field("Оперативная память, ГБ") + " ГБ")
            if field("Оперативная память, ГБ") else "не указана",
            "cpu": field("Процессор") or "не указан",
            "storage": (f"{storage or 'тип не указан'} — {size} ГБ"
                        + (" суммарно; разбивка неизвестна" if "+" in storage else ""))
            if size else (storage + " — объём не указан" if storage else "не указан")}

    # Model extraction only fills gaps; explicit owner observations take precedence.
    for key, value in card.get("semantic_specs", {}).items():
        if key in specs and (specs[key] is None or str(specs[key]).startswith("не указан")):
            specs[key] = value
    specs.update(review(card.get("id", "")).get("specs", {}))
    return specs


def seller_info(card):
    profile = card.get("seller_text", "")
    description = card.get("description", "")
    # Review count alone is not proof of a shop.
    if re.search(r"компания|магазин|центр электроники", profile, re.I):
        return {"type": "shop", "label": "Магазин / компания", "evidence": profile[:180]}
    if re.search(r"store|lab\b|центр|ноутбуки|электроник", profile, re.I):
        return {"type": "shop", "label": "Вероятно магазин", "evidence": "торговое название: " + profile[:150]}
    if re.search(r"магазин|работаем с ип и ооо|работаем с физ|наш[и ]+преимущества", description, re.I):
        return {"type": "shop", "label": "Вероятно магазин", "evidence": "признаки торговли в описании"}
    if re.search(r"частное лицо", profile, re.I):
        return {"type": "private", "label": "Частное лицо (по профилю Авито)", "evidence": profile[:180]}
    return {"type": "unknown", "label": "Тип продавца не определён", "evidence": ""}


def description_warnings(description, parameters=""):
    # A footer linking to the shop's other listings is not an assortment offer.
    description = re.sub(r"ссылка на все ноутбуки[^\n]*", "", description, flags=re.I)
    text = " ".join(description.lower().split())
    warnings = []
    if re.search(r"(?:цена|цены|стоимость|ноутбуки|игровые).{0,60}\bот\s*\d|\bот\s*\d[\d\s.,]*(?:₽|руб)", text):
        warnings.append("цена «от», стоимость выбранной модели нужно уточнить")
    if re.search(r"например|пример ноутбука", text):
        warnings.append("характеристики приведены как пример, конкретный ноутбук не подтверждён")
    if re.search(r"в ассортименте|большой выбор|подбер[её]м|подберу|ноутбуки.{0,40}в наличии", text):
        warnings.append("в описании предлагается ассортимент или подбор")
    if re.search(r"в наличии.{0,45}(?:конфигурации|комплектации)|"
                 r"(?:разные|различные|несколько)\s+(?:конфигураци|комплектаци)|"
                 r"цена.{0,30}зависит от.{0,20}комплектац", text):
        warnings.append("предлагаются разные комплектации; конкретный ноутбук за эту цену не определён")
    if re.search(r"характеристик.{0,160}(?:неверн|неправильн|ошиб|читайте|смотрите|не соответств)|"
                 r"(?:неверн|неправильн|ошиб).{0,80}характеристик", text):
        warnings.append("продавец просит не полагаться на характеристики Авито; сверить описание")
    gpu = lambda value: set(re.findall(r"\b(?:rtx|gtx)\s*(\d{3,4}(?:\s*ti)?)", gpu_text(value).lower()))
    if gpu(text) and gpu(parameters) and gpu(text).isdisjoint(gpu(parameters)):
        warnings.append("модель GTX/RTX в описании отличается от характеристик")
    prefix = gpu_text(description.split("\n", 1)[0][:120]).lower()
    first_gpu = re.search(r"\b(?:rtx|gtx)\s*\d{3,4}(?:\s*ti)?", prefix)
    if first_gpu:
        prefix = prefix[:first_gpu.end()]
    if (gpu(prefix) and gpu(parameters) and gpu(prefix).isdisjoint(gpu(parameters))
            and not re.search(r"лучше|быстрее|мощнее|сравнен", prefix)
            and not any("модель GTX/RTX" in warning for warning in warnings)):
        warnings.append("модель GTX/RTX в начале описания отличается от характеристик")
    specs = specifications({"parameters": parameters})
    # Compare only a single SSD against an explicitly stated SSD capacity.
    # A total for HDD+SSD cannot be compared to the SSD component alone.
    if specs["storage"].startswith("SSD — "):
        stated = re.findall(r"\bssd\D{0,12}(\d+)\s*(?:гб|gb|gв)", text)
        capacity = re.search(r"— (\d+) ГБ", specs["storage"])
        if stated and capacity and set(stated) != {capacity.group(1)}:
            warnings.append("объём SSD в описании отличается от характеристик")
    return warnings


def classify(item, max_price=65000, card=None):
    card = dict(card or {})
    identifier = identify(item, card)
    if identifier:
        card["id"] = identifier
    # ID-only metadata does not mean the actual card was read.
    has_content = bool(card.get("parameters") or card.get("description") or card.get("parsed"))
    title = str(item.get("title") or "")
    text = title + " " + str(item.get("description") or "")
    price = (item.get("priceDetailed") or {}).get("value")
    kazan = urlsplit(item.get("urlPath") or "").path.startswith("/kazan/")
    gaming = bool(GAMING.search(gpu_text(text)))
    excluded = bool(PARTS.search(title))
    bulk = bool(BULK.search(title))
    within_budget = type(price) in (int, float) and MIN_PRICE <= price <= max_price
    specs = specifications(card if has_content or review(identifier).get("specs") else {}, text)
    seller = seller_info(card or {})
    condition = "unknown"
    warnings = []
    if card:
        warnings = description_warnings(card.get("description", ""), card.get("parameters", ""))
        params = card.get("parameters", "").lower()
        if re.search(r"состояние\s*:?\s*(?:б/у|бывш\w* в употреблении)", params):
            condition = "used"
        elif re.search(r"состояние\s*:?\s*нов", params):
            condition = "new"
    gpu = specs["gpu"]
    models = gpu_models(gpu)
    supported = (len(models) == 1 and models[0].startswith("RTX ")
                 and int(re.search(r"\d+", models[0])[0]) in GPU_MODELS)
    gpu_known = gpu != "не указана"
    reasons = list(warnings)
    for risk in (card.get("semantic_analysis") or {}).get("payload", {}).get("risks", []):
        if risk["code"] in ("assortment", "contradiction"):
            reasons.append("Требует ручной проверки по разбору DeepSeek: " + risk["evidence"])
    if len(models) > 1:
        reasons.append("перечислены разные дискретные видеокарты; комплектация не определена")
    if gpu_known and not supported:
        reasons.append("видеокарта не входит в поддерживаемые RTX 2070 или RTX 3050+")
    if has_content and not gpu_known:
        reasons.append("конкретная дискретная видеокарта не подтверждена")
    candidate = (kazan and (gaming or supported) and within_budget and not excluded and not bulk
                 and condition != "new" and not reasons)
    # A plain category title is a reason to inspect, not proof of a gaming GPU.
    needs_details = (urlsplit(item.get("urlPath") or "").path.startswith("/kazan/noutbuki/")
                     and within_budget and not excluded and not bulk
                     and not re.search(r"\bmacbook\b|макбук", title, re.I)
                     and condition != "new" and not reasons and not has_content and not gpu_known)
    return {"candidate": candidate, "specs": specs, "seller": seller,
            "needs_details": needs_details,
            "rejection_reasons": reasons, "notify_ready": candidate and bool(card and card.get("parsed")) and supported,
            "kazan_url": kazan, "gaming_hint": gaming, "within_budget": within_budget,
            "excluded_title": excluded, "bulk_offer": bulk, "condition": condition,
            "warnings": warnings}


def choose_details(listings, seen, limit, max_price=65000):
    from price_signals import listing_hint
    candidates = []
    for item in listings:
        card = (seen.get(str(item["id"]), {}).get("card") or {})
        current = card if (card.get("reviewed_price") is None
                           or card.get("reviewed_price") == (item.get("priceDetailed") or {}).get("value")) else None
        flags = classify(item, max_price, current)
        if flags["candidate"] or flags["needs_details"]:
            candidates.append(item)
    def priority(item):
        card = (seen.get(str(item["id"]), {}).get("card") or {})
        price = (item.get("priceDetailed") or {}).get("value")
        needs_review = not card.get("parsed") or card.get("reviewed_price") != price
        flags = classify(item, max_price, card or None)
        gpu = flags["specs"]["gpu"].lower()
        return (0 if needs_review else 1, 0 if listing_hint(item) else 1,
                {"private": 0, "unknown": 1, "shop": 2}[flags["seller"]["type"]],
                0 if "4050" in gpu else 1 if "rtx" in gpu else 2,
                card.get("checked_at", ""), seen.get(str(item["id"]), {}).get("first_seen", "9999"))
    return sorted(candidates, key=priority)[:limit]


def parse_card(html):
    soup = BeautifulSoup(html, "html.parser")
    def text(selector):
        return "\n".join(x.get_text(" ", strip=True) for x in soup.select(selector))[:12000]
    title = text('[data-marker="item-view/title-info"], h1')
    description = text('[data-marker="item-view/item-description"]')
    parameters = text('[data-marker="item-view/item-params"]')
    address = text('[data-marker="item-view/item-address"]')
    seller_text = text('[data-marker*="seller-info"], [data-marker*="seller-rating"], [data-marker*="seller-link"], [data-marker*="seller-name"], [data-marker*="seller-type"]')
    return {"title": title, "description": description, "parameters": parameters,
            "seller_text": seller_text,
            "address": address, "parsed": bool(title and (description or parameters))}
