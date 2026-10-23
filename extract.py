"""Extract only saved live DOM; no network requests. Research data, not picks."""
import argparse
import json
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup


def extract(html, checked_at):
    soup = BeautifulSoup(html, 'html.parser')
    catalog = soup.select_one('[data-marker="catalog-serp"]')
    if catalog is None:
        raise ValueError('catalog-serp missing; not an empty successful search')
    items, other = [], []
    seen = set()
    for node in catalog.select('[data-marker="item"]'):
        identifier = node.get('data-item-id')
        title = node.select_one('[data-marker="item-title"]')
        price = node.select_one('[itemprop="price"]')
        if not identifier or title is None or price is None:
            raise ValueError('incomplete listing in saved DOM')
        if identifier in seen:
            continue
        seen.add(identifier)
        parts = urlsplit(urljoin('https://www.avito.ru', title['href']))
        if parts.hostname != 'www.avito.ru' or not parts.path.endswith('_' + identifier):
            raise ValueError('ID / URL mismatch')
        city_slug = parts.path.split('/')[1]
        location = node.select_one('[data-marker="item-location"]')
        row = dict(id=identifier, url=urlunsplit((parts.scheme, parts.netloc, parts.path, '', '')),
                   title=title.get_text(' ', strip=True), price=int(price['content']),
                   city='Казань' if city_slug == 'kazan' else None,
                   city_slug=city_slug, city_evidence='listing URL path; card unverified',
                   location_detail=location.get_text(' ', strip=True) or None if location else None,
                   checked_at=checked_at, card_verified=False,
                   source='saved live Chromium search DOM')
        (items if city_slug == 'kazan' else other).append(row)
    return {'local': items, 'other_cities': other, 'total': len(seen)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    meta = json.loads((args.directory / 'result.json').read_text())
    data = extract((args.directory / 'page.html').read_text(), meta['checked_at'])
    (args.directory / 'extracted.json').write_text(json.dumps(data, ensure_ascii=False, indent=2))
    # Gaming titles first for a useful diagnostic sample, not a recommendation.
    local = sorted(data['local'], key=lambda r: 'игров' not in r['title'].lower())
    sample = [r for r in local if r['price'] <= 50000][:20]
    (args.directory / 'sample.json').write_text(json.dumps(sample, ensure_ascii=False, indent=2))
    lines = ['# Реальные объявления из сохранённой выдачи', '',
             f"Проверка страницы: {meta['checked_at']}. Карточки не открыты. Это исследовательская выборка, не список рекомендаций.",
             'Город взят из URL объявления; точное местоположение требует сверки карточки. Не все ноутбуки игровые или б/у.', '',
             '| ID | Название / ссылка | Цена, ₽ | Местоположение |',
             '|---|---|---:|---|']
    for r in sample:
        title = r['title'].replace('|', '/')
        loc = 'Казань (URL); ' + (r['location_detail'] or 'адрес не указан в выдаче')
        lines.append(f"| {r['id']} | [{title}]({r['url']}) | {r['price']} | {loc} |")
    (args.directory / 'sample.md').write_text('\n'.join(lines) + '\n')
    print(json.dumps({'total': data['total'], 'local': len(data['local']),
                      'other_cities': len(data['other_cities']), 'sample': len(sample)}))
