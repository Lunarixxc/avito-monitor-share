"""Read-only page facts and fair queues for new listings and watched IDs."""
import datetime as dt
import re
from bs4 import BeautifulSoup
from filters import classify, choose_details, MIN_PRICE
from feedback import review


def timestamp(value):
    try:
        result = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result if result.tzinfo else result.replace(tzinfo=dt.timezone.utc)
    except (ValueError, TypeError, AttributeError):
        return dt.datetime.min.replace(tzinfo=dt.timezone.utc)


def page_facts(html, status=200):
    if status in (404, 410):
        return {'availability': 'missing', 'price': None, 'status_evidence': f'HTTP {status}'}
    if status != 200:
        return {'availability': 'unknown', 'price': None, 'status_evidence': f'HTTP {status}'}
    soup = BeautifulSoup(html, 'html.parser')
    # Seller descriptions / recommended cards cannot declare this item sold.
    nodes = soup.select('[data-marker="item-view/item-status"], [data-marker="item-view/status"], '
                        '[data-marker="item-status"], [data-marker="item-view/closed-warning"], h1, title')
    status_text = ' '.join(n.get_text(' ', strip=True) for n in nodes)
    if re.search(r'доступ ограничен|проверка браузера|подтвердите.{0,30}не робот|captcha', status_text, re.I):
        return {'availability': 'blocked', 'price': None, 'status_evidence': status_text[:200]}
    for state, pattern in [('sold', r'\bпродано\b|товар продан'),
                           ('removed', r'объявление\s+(?:снято с публикации|снято|закрыто|удалено|больше не доступно)')]:
        match = re.search(pattern, status_text, re.I)
        if match:
            return {'availability': state, 'price': None, 'status_evidence': match[0]}
    prices = set()
    for node in soup.select('meta[itemprop="price"], meta[property="product:price:amount"], '
                            '[data-marker="item-view/item-price"]'):
        value = node.get('content') or node.get_text(' ', strip=True)
        value = value.replace('\xa0', '').replace(' ', '').replace('₽', '').strip()
        if re.fullmatch(r'\d+(?:\.0+)?', value):
            number = int(float(value))
            if number > 0:
                prices.add(number)
    price = prices.pop() if len(prices) == 1 else None
    active = bool(price and soup.select_one('[data-marker="item-view/title-info"], h1'))
    return {'availability': 'active' if active else 'unknown', 'price': price,
            'status_evidence': 'card_price_and_title' if active else 'unrecognized_page'}


def as_listing(entry):
    return {'id': entry['id'], 'title': entry['title'], 'urlPath': entry['url'],
            'priceDetailed': {'value': entry['price']}}


def catalog_coverage(result, previous):
    """Track a bounded category sweep; overlap is never proof of full coverage."""
    same_catalog = previous.get('catalog') == result.get('catalog')
    pages = result.get('pages', [])
    fresh_ids = list(dict.fromkeys(str(identifier) for page in pages
                                  if int(page['page']) <= 3 for identifier in page['ids']))
    old_ids = set(previous.get('fresh_ids', [])) if same_catalog else set()
    overlap = len(old_ids.intersection(fresh_ids))
    if not fresh_ids:
        window = 'empty'
    elif not old_ids:
        window = 'initial'
    else:
        window = 'overlap' if overlap else 'no_overlap'
    deep = [page for page in pages if int(page['page']) >= 4]
    next_page = previous.get('next_page', 4) if same_catalog else 4
    deep_ids = previous.get('deep_ids', []) if same_catalog else []
    sweep = 'paused' if not deep else 'progress'
    if deep:
        last = max(deep, key=lambda page: int(page['page']))
        current_ids = [str(identifier) for identifier in last['ids']]
        if last.get('http_status') in (400, 404):
            next_page, sweep = 4, 'page_unavailable'
        elif not current_ids:
            next_page, sweep = 4, 'empty_page'
        elif deep_ids and set(current_ids) == set(deep_ids):
            # Some catalog responses repeat their final page instead of ending.
            next_page, sweep = 4, 'repeated_page'
        else:
            next_page = int(last['page']) + 1
        deep_ids = current_ids
    return {'catalog': result['catalog'], 'fresh_ids': fresh_ids, 'fresh_window': window,
            'overlap_count': overlap, 'pages': [int(page['page']) for page in pages],
            'next_page': next_page, 'deep_ids': deep_ids, 'sweep_status': sweep,
            'full_coverage': False}


def review_queue(listings, seen, limit, max_price=65000, now=None, for_digest=False):
    from price_signals import listing_hint
    if limit <= 0:
        return []
    now = timestamp(now or dt.datetime.now(dt.timezone.utc).isoformat())
    # Unread category cards survive dropping out of the bounded fresh window.
    inputs = list(listings)
    search_items = {str(item['id']): item for item in listings}
    search_ids = set(search_items)
    inputs.extend(as_listing(entry) for entry in seen.values()
                  if str(entry['id']) not in search_ids and not (entry.get('card') or {}).get('parsed')
                  and (now - timestamp(entry.get('last_seen'))).days <= 30)
    discovery = choose_details(inputs, seen, len(inputs), max_price)
    discovery = [x for x in discovery if not (
        seen.get(str(x['id']), {}).get('availability') in ('sold', 'removed', 'unavailable')
        and (now - timestamp(seen[str(x['id'])].get('availability_checked_at'))).total_seconds() < 86400)]
    # Known URLs stay in rotation even after dropping out of search results.
    watches = []
    watch_flags = {}
    for entry in seen.values():
        visible_price = (search_items.get(str(entry['id']), {}).get('priceDetailed') or {}).get('value')
        if type(visible_price) in (int, float) and visible_price < MIN_PRICE:
            continue
        if entry.get('availability') in ('sold', 'removed', 'unavailable'):
            # Check occasionally for republishing under the same ID.
            if (now - timestamp(entry.get('availability_checked_at'))).total_seconds() < 86400:
                continue
        if (now - timestamp(entry.get('last_seen'))).days > 30:
            continue
        listing = as_listing(entry)
        flags = classify(listing, max_price, entry.get('card'))
        if flags['candidate'] and (entry.get('card') or {}).get('parsed'):
            watches.append(listing)
            watch_flags[str(entry['id'])] = flags
    def priority(item):
        old = seen[str(item['id'])]
        needs_manual_price = (review(item['id']).get('reported_price') is not None
                              and not old.get('card', {}).get('price_source') == 'card')
        return (0 if needs_manual_price else 1, old.get('availability_checked_at', ''),
                (old.get('card') or {}).get('checked_at', ''))
    watches.sort(key=priority)
    chosen, ids = [], set()
    def take(pool, count):
        added = 0
        for item in pool:
            if str(item['id']) in ids:
                continue
            ids.add(str(item['id'])); chosen.append(item); added += 1
            if added >= count:
                break
    # Before the digest, refresh the strongest known options before discovery.
    # Closed IDs stay in the ordinary rotation, never ahead of viable options.
    if for_digest:
        from ranking import rank
        ranked = [x for x in watches if seen[str(x['id'])].get('availability')
                  not in ('sold', 'removed', 'unavailable')
                  and watch_flags[str(x['id'])].get('notify_ready')]
        ranked.sort(key=lambda x: (
            0 if (now - timestamp((seen[str(x['id'])].get('card') or {}).get('checked_at'))).total_seconds() >= 7200 else 1,
            -rank({**seen[str(x['id'])], 'flags': watch_flags[str(x['id'])]})['score'], str(x['id'])))
        take(ranked, min(10, limit))
    # Read a few unusually cheap new cards before the ordinary watch rotation.
    if not for_digest:
        take([item for item in discovery if listing_hint(item)], min(3, limit))
    # Reserve half the capacity for known candidates. Spare slots go to either queue.
    if watches and not for_digest and len(chosen) < limit:
        take(watches, min(max(1, limit // 2), limit - len(chosen)))
    # Reserve exploration slots so known gaming names cannot starve plain titles.
    if len(chosen) < limit:
        unknown = [item for item in discovery
                   if classify(item, max_price, seen.get(str(item['id']), {}).get('card'))['needs_details']]
        take(unknown, min(2, limit - len(chosen)))
    if len(chosen) < limit:
        take(discovery, limit - len(chosen))
    if len(chosen) < limit:
        take(watches, limit - len(chosen))
    return chosen
