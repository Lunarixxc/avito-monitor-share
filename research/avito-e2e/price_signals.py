"""Conservative owner-calibrated TOP price gates for used gaming laptops."""
import re

from analysis import facts, risk_signals


# Kazan, 2026-09-29. Base case: a working H/HS/HX CPU, 16 GB RAM, 512 GB SSD.
# The small local sample is asking prices, so these are alert gates, not valuations.
TOP_BASE_RUB = {
    'RTX 3050': 40000,
    'RTX 3050TI': 42000,
    'RTX 3060': 50000,
    'RTX 2070': 47500,
    'RTX 4050': 52000,
    'RTX 3070': 56000,
    'RTX 3070TI': 58000,
    'RTX 4060': 59000,
    'RTX 3080': 60000,
    'RTX 4070': 65000,
}
MIN_VRAM_GB = {'RTX 2070': 8, 'RTX 3050': 4, 'RTX 3050TI': 4,
               'RTX 3060': 6, 'RTX 3070': 8, 'RTX 3070TI': 8,
               'RTX 3080': 8, 'RTX 4050': 6, 'RTX 4060': 8, 'RTX 4070': 8}


def listing_hint(item):
    """Search data only changes card-reading order; it never triggers an alert."""
    title = str(item.get('title') or '').upper()
    price = (item.get('priceDetailed') or {}).get('value')
    if type(price) not in (int, float) or price <= 0:
        return False
    for gpu, base in TOP_BASE_RUB.items():
        model = gpu.replace('TI', r'\s*TI').replace(' ', r'[\s-]*')
        if price <= min(65000, base + 4000) and re.search(model + r'\b', title):
            return True
    return False


def top_price(item):
    """Return the adjusted gate for a verified, reasonably complete laptop."""
    card = item.get('card') or {}
    price = item.get('price')
    if (not item.get('flags', {}).get('notify_ready') or item.get('availability') != 'active'
            or type(price) not in (int, float) or card.get('price_source') != 'card'
            or card.get('reviewed_price') != price or not card.get('parsed')):
        return None
    risks = risk_signals(card)
    if any(r['code'] in ('repair', 'defect') for r in risks):
        return None
    observed = facts(item)
    gpu = observed['gpu']
    if gpu not in TOP_BASE_RUB or (observed['vram_gb'] or 0) < MIN_VRAM_GB[gpu]:
        return None
    cpu = observed['cpu'] or ''
    if not re.search(r'(?:H|HS|HX)$', cpu, re.I) or not re.search(r'^(?:i[579]|ryzen[579]|(?:core|ultra)[579])', cpu):
        return None
    ram = observed['ram_gb'] or 0
    storage = re.fullmatch(r'SSD — (\d+) ГБ', observed['storage'])
    if ram < 8 or not storage or int(storage[1]) < 256:
        return None
    gate = TOP_BASE_RUB[gpu]
    gate += -2000 if ram < 16 else 2000 if ram >= 32 else 0
    gate += -1000 if int(storage[1]) < 512 else 1000 if int(storage[1]) >= 1000 else 0
    gate -= 2000 * sum(r['code'] == 'peripheral' for r in risks)
    if gpu == 'RTX 3050' and observed['vram_gb'] >= 6:
        gate += 1000
    # A declared low power GPU is materially slower than the average variant.
    tgp = re.search(r'\bTGP\s*[:=]?\s*(\d{2,3})\s*(?:W|Вт)\b',
                    card.get('description', '') + ' ' + card.get('parameters', ''), re.I)
    if tgp and int(tgp[1]) < 60:
        gate -= 4000
    return {'gpu': gpu, 'gate_rub': min(65000, gate), 'risks': risks}


def strong_price(item):
    result = top_price(item)
    return result if result and item['price'] <= result['gate_rub'] else None


def message(item):
    signal = strong_price(item)
    if not signal:
        return ''
    caveats = [r['label'] for r in signal['risks'] if r['code'] == 'peripheral']
    warning = '\n⚠️ ' + '; '.join(caveats) + '. Уточнить до покупки.' if caveats else ''
    return (f"🔥 ТОП-цена по вашему ориентиру: {item['price']:g} ₽; "
            f"порог для {signal['gpu']} и этой конфигурации — {signal['gate_rub']:g} ₽. "
            'Цена и доступность проверены по карточке. TGP и состояние проверить лично.' + warning)
