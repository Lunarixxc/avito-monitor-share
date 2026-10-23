"""One CDP navigation in a manually verified Windows Chrome. No live default."""
import argparse
import datetime as dt
import importlib.metadata
import json
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from urllib.request import ProxyHandler, build_opener

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

ROOT = Path(__file__).resolve().parent
ENDPOINT = 'http://127.0.0.1:9222'
SEARCH = 'https://www.avito.ru/kazan/noutbuki'
ITEM = '[data-marker="catalog-serp"] [data-marker="item"]'
BLOCKS = ('проблема с ip', 'подтвердите, что вы не робот', 'проверка браузера',
          'доступ ограничен', 'слишком много запросов', 'проверка безопасности',
          'captcha', 'ip_blocked')


def safe_url(value):
    """Never persist arbitrary redirect paths, query strings or fragments."""
    p = urlsplit(value)
    if p.scheme != 'https' or p.hostname not in ('avito.ru', 'www.avito.ru'):
        return '[non-Avito URL omitted]'
    path = p.path if p.path == '/kazan/noutbuki' else '/[path omitted]'
    return urlunsplit(('https', p.hostname, path, '', ''))


def endpoint_check():
    # No system proxy and no request to Avito; do not print WebSocket identifiers.
    with build_opener(ProxyHandler({})).open(ENDPOINT + '/json/version', timeout=5) as r:
        data = json.load(r)
    ws = urlsplit(data.get('webSocketDebuggerUrl', ''))
    if ws.scheme != 'ws' or ws.hostname not in ('127.0.0.1', 'localhost', '::1') or ws.port != 9222:
        raise ValueError('unexpected_cdp_endpoint')
    return {'browser': data.get('Browser'), 'protocol': data.get('Protocol-Version'),
            'websocket_loopback': True}


def snapshot(page):
    # Read bounded technical facts, never HTML, storage, headers or console text.
    return page.evaluate('''(markers) => {
      const text = (document.body?.innerText || '').toLowerCase();
      return {ready_state: document.readyState,
        catalog_present: !!document.querySelector('[data-marker="catalog-serp"]'),
        dom_count: document.querySelectorAll('[data-marker="catalog-serp"] [data-marker="item"]').length,
        block_markers: markers.filter(m => text.includes(m)),
        webdriver: navigator.webdriver, language: navigator.language,
        viewport: {width: innerWidth, height: innerHeight},
        visibility: document.visibilityState};
    }''', BLOCKS)


def extract_rows(page):
    # DOM iteration only: no scrolling, clicks, detail pages or network loops.
    return page.locator(ITEM).evaluate_all('''nodes => nodes.slice(0, 5).flatMap(n => {
      const a = n.querySelector('[data-marker="item-title"]');
      const id = n.getAttribute('data-item-id');
      if (!a || !/^\\d+$/.test(id || '')) return [];
      const u = new URL(a.href, location.href);
      if (!['www.avito.ru','avito.ru'].includes(u.hostname) || !u.pathname.endsWith('_' + id)) return [];
      return [{id, title: (a.textContent || '').trim().slice(0, 250),
        url: u.origin + u.pathname,
        price: n.querySelector('[itemprop="price"]')?.getAttribute('content') || null}];
    })''')


def run(report):
    report['endpoint'] = endpoint_check()
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(ENDPOINT, timeout=10000,
                                               no_defaults=True, is_local=False)
        # Do not create another context or close the owner's context/browser.
        if len(browser.contexts) != 1:
            report['outcome'] = 'unexpected_context_count'
            return
        context = browser.contexts[0]
        pages = [p for p in context.pages if p.url == SEARCH]
        if len(pages) != 1:
            report['outcome'] = 'prepare_exactly_one_control_tab'
            return
        page = pages[0]
        report['chrome_version'] = browser.version
        report['after_attach'] = snapshot(page)
        if report['after_attach']['block_markers']:
            report['outcome'] = 'manual_ok_attach_blocked'
            return
        if not report['after_attach']['catalog_present'] or not report['after_attach']['dom_count']:
            report['outcome'] = 'control_dom_unconfirmed'
            return
        phase = 'navigation'

        def response_seen(response):
            request = response.request
            if request.is_navigation_request() and response.frame == page.main_frame:
                report['documents'].append({'status': response.status,
                                            'url': safe_url(response.url)})
            if response.status in (403, 429, 439):
                key = str(response.status)
                report['restricted_responses'][key] = report['restricted_responses'].get(key, 0) + 1

        page.on('response', response_seen)
        try:
            report['goto_calls'] = 1
            response = page.goto(SEARCH, wait_until='domcontentloaded', timeout=30000)
            report['http_status'] = response.status if response else None
            immediate = snapshot(page)
            if not immediate['block_markers'] and report['http_status'] not in (403, 429, 439):
                phase = 'dom_wait'
                # Bounded DOM polling only, not repeated requests. No networkidle wait.
                page.wait_for_function('''markers => {
                  const text = (document.body?.innerText || '').toLowerCase();
                  return markers.some(m => text.includes(m)) ||
                    document.querySelector('[data-marker="catalog-serp"] [data-marker="item"] [data-marker="item-title"]');
                }''', arg=BLOCKS, timeout=15000)
        except PlaywrightTimeout:
            report['timeout_phase'] = phase
        except Exception as exc:
            report['navigation_error_type'] = type(exc).__name__
        finally:
            report['final_url'] = safe_url(page.url)
            report['after_navigation'] = snapshot(page)
            state = report['after_navigation']
            blocked = (bool(state['block_markers'])
                       or report.get('http_status') in (403, 429, 439)
                       or any(d['status'] in (403, 429, 439) for d in report['documents']))
            report['items'] = [] if blocked else extract_rows(page)
            report['result_count'] = len(report['items'])
            if blocked:
                report['outcome'] = 'manual_ok_cdp_navigation_blocked'
            elif report.get('timeout_phase') or report.get('navigation_error_type'):
                report['outcome'] = 'inconclusive_navigation_error'
            elif report['restricted_responses']:
                report['outcome'] = 'inconclusive_partial_restriction'
            elif (report.get('http_status') == 200 and page.url == SEARCH
                  and state['catalog_present'] and report['items']):
                report['outcome'] = 'success'
            else:
                report['outcome'] = 'inconclusive_dom_or_http'
        # sync_playwright stops its driver/disconnects; Chrome and tab remain open.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-endpoint', action='store_true')
    parser.add_argument('--run-once', action='store_true')
    parser.add_argument('--manual-result', choices=('works', 'blocked'))
    args = parser.parse_args()
    if args.check_endpoint:
        try:
            print(json.dumps(endpoint_check()))
            return 0
        except Exception as exc:
            print(json.dumps({'error_type': type(exc).__name__}))
            return 2
    if not args.run_once or args.manual_result is None:
        parser.error('Use --check-endpoint or --run-once --manual-result works|blocked')
    os.umask(0o077)
    output = ROOT / 'results'
    output.mkdir(mode=0o700, exist_ok=True)
    # Exclusive persistent claim prevents concurrent or accidental repeated runs.
    try:
        (output / 'attempt.claim').touch(exist_ok=False)
    except FileExistsError:
        print('Attempt already claimed; inspect results. No connection made.')
        return 2
    report = dict(started_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                  manual_result=args.manual_result, manual_evidence='operator observation before CDP',
                  playwright_version=importlib.metadata.version('playwright'),
                  search_url=SEARCH, outcome='environment_error', goto_calls=0,
                  documents=[], restricted_responses={}, items=[], result_count=0)
    try:
        if args.manual_result == 'blocked':
            report['outcome'] = 'manual_failed_no_cdp'
        else:
            run(report)
    except Exception as exc:
        report['error_type'] = type(exc).__name__  # Exception text can contain secrets.
    finally:
        report['finished_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
        (output / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'outcome': report['outcome'], 'result_count': report['result_count'],
                          'report': str(output / 'result.json')}, ensure_ascii=False))
    return 0 if report['outcome'] == 'success' else 2


if __name__ == '__main__':
    raise SystemExit(main())
