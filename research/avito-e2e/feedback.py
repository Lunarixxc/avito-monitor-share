"""Explicit user observations; never overwrite raw prices or fetch timestamps."""
import json
from functools import lru_cache
from pathlib import Path

PATH = Path(__file__).resolve().parent / 'data/manual_reviews.json'


@lru_cache(maxsize=1)
def reviews():
    return json.loads(PATH.read_text())['items'] if PATH.exists() else {}


def review(identifier):
    return reviews().get(str(identifier), {})


def identify(item, card=None):
    import re
    value = item.get('id') or (card or {}).get('id')
    if not value:
        match = re.search(r'_(\d+)(?:\?|$)', item.get('urlPath', '') or item.get('url', ''))
        value = match[1] if match else ''
    return str(value)
