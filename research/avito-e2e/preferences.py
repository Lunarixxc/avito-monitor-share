"""Owner's listing choices, stored with the monitor state under its shared lock."""
import fcntl
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit


def listing_id(value):
    value = (value or '').strip()
    if re.fullmatch(r'\d+', value):
        return value
    parsed = urlsplit(value)
    if parsed.hostname not in {'avito.ru', 'www.avito.ru'}:
        return None
    match = re.search(r'_(\d+)$', parsed.path.rstrip('/'))
    return match[1] if match else None


def hidden(state, identifier):
    return str(identifier) in state.get('hidden', {})


def change(folder, identifier, action, now, expected_price=None):
    """Make an atomic choice while sharing the collector/digest file lock."""
    identifier = str(identifier)
    if not re.fullmatch(r'\d+', identifier):
        return 'invalid'
    with (folder / 'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = folder / 'state.json'
        if not path.exists():
            return 'missing'
        state = json.loads(path.read_text())
        item = state.get('seen', {}).get(identifier)
        if not item:
            return 'missing'
        choices = state.setdefault('hidden', {})
        if action == 'hide':
            card = item.get('card') or {}
            price = card.get('reviewed_price')
            if (card.get('price_source') != 'card' or type(price) not in (int, float)
                    or price <= 0):
                return 'unverified'
            if expected_price is not None and price != expected_price:
                return 'stale'
            if choices.get(identifier, {}).get('price') == price:
                return 'already_hidden'
            choices[identifier] = {'price': price, 'at': now}
        elif action == 'show':
            if identifier not in choices:
                return 'already_visible'
            del choices[identifier]
        else:
            return 'invalid'
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2))
        os.replace(temporary, path)
        return 'hidden' if action == 'hide' else 'visible'
