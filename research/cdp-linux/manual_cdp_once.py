"""One navigation in manually confirmed Linux Chrome; observe natural transitions."""
import argparse
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import time
from urllib.parse import urldefrag

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('helpers', ROOT.parent / 'cdp-windows/probe.py')
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)


def run(report):
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp('http://127.0.0.1:9223',
                                               no_defaults=True, timeout=10000)
        report['chrome_version'] = browser.version
        if len(browser.contexts) != 1:
            report['outcome'] = 'unexpected_context_count'
            return
        pages = [p for p in browser.contexts[0].pages if urldefrag(p.url).url == helpers.SEARCH]
        if len(pages) != 1:
            report['outcome'] = 'control_tab_not_found'
            return
        page = pages[0]
        report['after_attach'] = helpers.snapshot(page)
        if report['after_attach']['block_markers'] or not report['after_attach']['dom_count']:
            report['outcome'] = 'control_not_confirmed_after_attach'
            return
        started = time.monotonic()

        def record(response):
            if response.request.is_navigation_request() and response.frame == page.main_frame:
                report['document_count'] += 1
                report['last_document_status'] = response.status
                if len(report['documents']) < 100:
                    report['documents'].append(dict(seconds=round(time.monotonic() - started, 3),
                                                    status=response.status,
                                                    url=helpers.safe_url(response.url)))
            if response.status in (403, 429, 439):
                key = str(response.status)
                report['restricted_responses'][key] = report['restricted_responses'].get(key, 0) + 1

        page.on('response', record)
        report['goto_calls'] = 1
        try:
            response = page.goto(helpers.SEARCH, wait_until='domcontentloaded', timeout=30000)
            report['initial_status'] = response.status if response else None
        except Exception as exc:
            report['navigation_error_type'] = type(exc).__name__
        try:
            report['immediate'] = helpers.snapshot(page)
        except Exception as exc:
            report['immediate_error_type'] = type(exc).__name__
        # Service the browser event loop while the page itself may change.
        # No reload/goto/retry, DOM modification, click, CAPTCHA interaction or API call.
        page.wait_for_timeout(20000)
        report['final_url'] = helpers.safe_url(page.url)
        report['final'] = helpers.snapshot(page)
        if report['final']['block_markers']:
            report['outcome'] = 'blocked_after_observation'
            return
        report['items'] = helpers.extract_rows(page)
        if (urldefrag(page.url).url == helpers.SEARCH and report['last_document_status'] == 200
                and report['final']['dom_count'] and report['items']):
            restricted = bool(report['restricted_responses']) or bool(
                report.get('immediate', {}).get('block_markers'))
            report['outcome'] = 'catalog_available_with_restriction_history' if restricted else 'success'
        else:
            report['outcome'] = 'inconclusive'
        # Stop Playwright only, leaving the manually owned Chrome session open.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-once', action='store_true')
    args = parser.parse_args()
    if not args.run_once:
        parser.error('--run-once required after manual confirmation')
    os.umask(0o077)
    output = ROOT / 'results' / 'manual-navigation'
    output.mkdir(parents=True, exist_ok=True)
    try:
        (output / 'attempt.claim').touch(exist_ok=False)
    except FileExistsError:
        parser.error('Attempt already claimed; no connection or navigation performed')
    report = dict(started_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                  manual_observation='Operator saw IP block briefly, then page transitioned to listings',
                  manual_http_status=None, outcome='environment_error', goto_calls=0,
                  observation_seconds=20, documents=[], document_count=0,
                  last_document_status=None, restricted_responses={}, items=[])
    try:
        run(report)
    except Exception as exc:
        report['error_type'] = type(exc).__name__
    finally:
        report['result_count'] = len(report['items'])
        report['finished_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
        (output / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'outcome': report['outcome'], 'result_count': report['result_count'],
                          'report': str(output / 'result.json')}, ensure_ascii=False))
    return 0 if report['outcome'] == 'success' else 2


if __name__ == '__main__':
    raise SystemExit(main())
