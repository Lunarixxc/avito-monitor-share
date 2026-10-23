"""Reconcile observations without inventing sales or card-price verification."""
from copy import deepcopy
import hashlib
import json
from filters import classify
from tracking import as_listing, timestamp, catalog_coverage

CLOSED = {'sold', 'removed', 'unavailable'}


def event(kind, item, at, previous_price=None):
    value = {'type': kind, 'id': item['id'], 'title': item['title'], 'url': item['url'],
             'price': item['price'], 'at': at, 'previous_price': previous_price}
    value['event_id'] = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:24]
    return value


def reconcile(previous, result, max_price=65000):
    if result.get('status') != 'success':
        raise ValueError('unsuccessful_snapshot')
    state = deepcopy(previous) if previous else {'seen': {}}
    seen = state['seen']; at = result['started']; baseline = not previous
    changes, events = [], []
    listings = {str(x['id']): x for x in result['listings']}
    cards = {str(x['id']): x for x in result.get('cards', [])}
    for key, card in cards.items():
        if key not in listings and card.get('listing'):
            listings[key] = card['listing']
    search_ids = {str(x['id']) for x in result['listings']}
    for key, listing in listings.items():
        old = previous.get('seen', {}).get(key, {})
        price = (listing.get('priceDetailed') or {}).get('value')
        if type(price) not in (int, float) or price <= 0:
            continue
        entry = seen.get(key, {})
        from urllib.parse import urlsplit
        entry.update({'id': key, 'title': listing.get('title', old.get('title', 'Ноутбук')),
                      'url': 'https://www.avito.ru' + urlsplit(listing.get('urlPath') or '').path,
                      'price': price, 'first_seen': old.get('first_seen', at)})
        if key in search_ids:
            entry['last_seen'] = at
            entry['search_price'] = price
        else:
            entry.setdefault('last_seen', old.get('last_seen', at))
        card = cards.get(key)
        if card:
            status = card.get('availability', 'unknown')
            entry['availability_checked_at'] = at
            entry['status_evidence'] = card.get('status_evidence', '')
            if status == 'missing':
                last_miss = timestamp(old.get('last_missing_at'))
                distinct = (timestamp(at) - last_miss).total_seconds() >= 900
                count = old.get('missing_count', 0) + int(distinct)
                entry.update(missing_count=count, last_missing_at=at)
                entry['availability'] = 'unavailable' if count >= 2 else 'unknown'
            else:
                entry['missing_count'] = 0
                entry['availability'] = status if status in CLOSED | {'active'} else 'unknown'
            if card.get('parsed'):
                entry['card'] = card
            else:
                entry.setdefault('card', old.get('card'))
            if status == 'active' and type(card.get('price')) in (int, float) and card['price'] > 0:
                entry['price'] = card['price']
                entry['price_source'] = 'card'
                entry['last_seen'] = at
            else:
                entry['price_source'] = 'search'
        else:
            entry.setdefault('card', old.get('card'))
            entry['price_source'] = 'search'
            # A search result cannot undo an explicit closed-card status.
            entry.setdefault('availability', 'unknown')
        entry['flags'] = classify(as_listing(entry), max_price, entry.get('card'))
        seen[key] = entry
        hidden_at_price = state.get('hidden', {}).get(key, {}).get('price')
        if (hidden_at_price is not None and card and card.get('availability') == 'active'
                and entry.get('price_source') == 'card' and entry['price'] != hidden_at_price):
            state['hidden'].pop(key, None)
        if entry['flags']['candidate']:
            if not baseline and not old:
                changes.append({'type': 'new', **entry})
            elif old and old['price'] != entry['price']:
                changes.append({'type': 'price_down' if entry['price'] < old['price'] else 'price_up',
                                'previous_price': old['price'], **entry})
        was_ready = old.get('flags', {}).get('notify_ready', False)
        if entry['availability'] in CLOSED and old.get('availability') != entry['availability'] and was_ready:
            events.append(event(entry['availability'], entry, at))
        if (entry['availability'] == 'active' and entry['price_source'] == 'card'
                and entry['flags']['notify_ready']):
            old_verified = old.get('verified_price')
            # Migrate legacy observed prices only for already reviewed candidates.
            if old_verified is None and was_ready:
                old_verified = old.get('price')
            if old.get('availability') in CLOSED:
                events.append(event('returned', entry, at))
            elif old_verified is None and not baseline:
                events.append(event('new', entry, at))
            if old_verified is not None and old_verified != entry['price']:
                kind = 'price_down' if entry['price'] < old_verified else 'price_up'
                events.append(event(kind, entry, at, old_verified))
            entry['verified_price'] = entry['price']
    # Reapply manual rules also to items not present in this limited search.
    for entry in seen.values():
        entry['flags'] = classify(as_listing(entry), max_price, entry.get('card'))
    pending = state.setdefault('pending_events', [])
    known = {e['event_id'] for e in pending}
    pending.extend(e for e in events if e['event_id'] not in known)
    state['pending_events'] = [e for e in pending if (timestamp(at) - timestamp(e['at'])).days <= 30]
    state['updated'] = at
    candidates = [x for x in seen.values() if x['flags']['candidate'] and x.get('availability') not in CLOSED]
    report = {'snapshot': at, 'baseline': baseline, 'pages': len(result.get('pages', [])),
              'cards_checked': len(cards), 'observed': len(result['listings']),
              'candidates': candidates, 'changes': changes, 'events': events}
    if result.get('search_mode') == 'category':
        scan = catalog_coverage(result, previous.get('catalog_scan', {}))
        state['catalog_scan'] = scan
        report['search_coverage'] = scan
        report['pending_details'] = sum(
            not (entry.get('card') or {}).get('parsed')
            and (entry['flags']['candidate'] or entry['flags']['needs_details'])
            and entry.get('availability') not in CLOSED
            and (timestamp(at) - timestamp(entry.get('last_seen'))).days <= 30
            for entry in seen.values())
    return state, report
