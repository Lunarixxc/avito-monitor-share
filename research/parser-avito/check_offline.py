"""Run reviewed upstream methods without importing its application/side effects."""
import ast
import contextlib
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from loguru import logger
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent.parent
UPSTREAM = ROOT / 'upstream'
sys.path[:0] = [str(UPSTREAM), str(PROJECT)]
from extract import extract
from models import ItemsResponse
from parser.http.client import HttpClient
from parser.proxies.proxy import NoProxy
from parser.url_converter import AvitoUrlConverter

logger.remove()


def upstream_method(name):
    """Compile exactly one reviewed method; do not run parser_cls imports/init."""
    source = UPSTREAM / 'parser_cls.py'
    tree = ast.parse(source.read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'AvitoParse')
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == name)
    method.decorator_list = []
    namespace = dict(globals())
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), 'exec'), namespace)
    return namespace[name]


def main():
    find_json = upstream_method('find_json_on_page')
    catalog_from_api = upstream_method('_extract_api_catalog')
    page_url = upstream_method('_api_url_for_page')
    fixture = PROJECT / 'results/2026-09-19T10-40-59.101759+00-00/page.html'
    html = fixture.read_text()
    data = find_json(html)
    catalog = data.get('catalog', {})
    raw_items = catalog.get('items', [])
    dom = extract(html, 'historical fixture 2026-09-19T10:40:59Z')
    dom_rows = {r['id']: r for r in dom['local'] + dom['other_cities']}
    json_rows = {str(r['id']): r for r in raw_items if r.get('id')}
    validation = {}
    try:
        parsed = ItemsResponse(**catalog)
        validation = {'ok': True, 'items': len(parsed.items)}
    except ValidationError as exc:
        validation = {'ok': False, 'error_count': exc.error_count(),
                      'locations': [list(e['loc']) for e in exc.errors()[:8]]}
    report = {
        'fixture': str(fixture.relative_to(PROJECT)),
        'fresh_network_results': False,
        'loader_data_keys': sorted(data),
        'json_items': len(raw_items), 'json_unique_ids': len(json_rows),
        'dom_items': len(dom_rows), 'dom_kazan': len(dom['local']),
        'same_id_set': set(dom_rows) == set(json_rows),
        'matching_prices': sum(r.get('priceDetailed', {}).get('value') == dom_rows[k]['price']
                               for k, r in json_rows.items() if k in dom_rows),
        'matching_titles': sum(r.get('title') == dom_rows[k]['title']
                               for k, r in json_rows.items() if k in dom_rows),
        'model_validation': validation,
        'blocked_pages': [],
    }
    soup = BeautifulSoup(html, 'html.parser')
    report['data_mfe_state_nodes'] = len(soup.select('[data-mfe-state]'))
    for script in soup.select('script'):
        if 'window.__staticRouterHydrationData = JSON.parse(' in script.text:
            encoded = script.text.split('JSON.parse(', 1)[1].rsplit(')', 1)[0]
            hydration = json.loads(json.loads(encoded))
            report['hydration_loader_keys'] = list(hydration.get('loaderData', {}))
            report['hydration_catalog_route_keys'] = list(
                hydration.get('loaderData', {}).get('catalog-or-main-or-item', {}))
    synthetic_data = {'catalog': {'items': [{'id': 123, 'title': 'Synthetic fixture'}]}}
    synthetic_state = {'i18n': {'hasMessages': True}, 'loaderData': {'data': synthetic_data}}
    synthetic_html = '<script type="mime/invalid" data-mfe-state="true">' + json.dumps(synthetic_state) + '</script>'
    assert find_json(synthetic_html) == synthetic_data
    report['legacy_parser_positive_fixture'] = 'passed (synthetic only)'
    assert len(ItemsResponse(**synthetic_data['catalog']).items) == 1
    report['minimal_api_model_fixture'] = 'passed; model accepts id/title without price or URL'
    for path in sorted((PROJECT / 'results').glob('*/page.html')):
        if path == fixture:
            continue
        report['blocked_pages'].append({'path': str(path.relative_to(PROJECT)),
                                       'returned_empty': find_json(path.read_text()) == {}})
    # Explicitly distinguish an extraction failure from an empty catalogue.
    report['missing_state_returns_empty_dict'] = find_json('<html>CAPTCHA</html>') == {}
    payload = {'items': [{'id': 123}]}
    assert all(catalog_from_api(p) == payload for p in
               [payload, {'catalog': payload}, {'result': payload}, {'result': {'catalog': payload}}])
    assert catalog_from_api({'error': 'blocked'}) == {}
    page2 = page_url('https://example.invalid/items?p=1&query=laptop', 2)
    assert dict(parse_qsl(urlsplit(page2).query)) == {'query': 'laptop', 'page': '2'}
    report['api_shapes_and_page_parameter'] = 'passed (synthetic, no live API fixture)'
    outcomes = []
    for status in (403, 429, 439, 200):
        proxy = Mock(spec=NoProxy)
        response = SimpleNamespace(status_code=status, url='https://example.invalid/',
                                   raise_for_status=lambda: None)
        session = Mock()
        session.request.return_value = response
        with patch.object(HttpClient, '_build_client', return_value=session), \
                patch('parser.http.client.time.sleep'), contextlib.redirect_stdout(io.StringIO()):
            client = HttpClient(proxy=proxy)
            try:
                client.request('GET', 'https://example.invalid/')
                outcome = 'returned_response'
            except RuntimeError:
                outcome = 'runtime_error'
        expected = 1 if status == 200 else 5
        assert session.request.call_count == expected
        assert proxy.handle_block.call_count == (0 if status == 200 else 1)
        outcomes.append({'status': status, 'requests': session.request.call_count,
                         'block_handler_calls': proxy.handle_block.call_count, 'outcome': outcome})
    report['http_mock_tests'] = outcomes
    assert report['missing_state_returns_empty_dict']
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
