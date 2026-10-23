"""One authorized Linux Google Chrome CDP attempt; no manual control claimed."""
import argparse
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import time

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('cdp_helpers', ROOT.parent / 'cdp-windows/probe.py')
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)
CHROME = Path('/opt/google/chrome/chrome')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-once', action='store_true')
    args = parser.parse_args()
    if not args.run_once:
        parser.error('Explicit --run-once required')
    os.umask(0o077)
    if not CHROME.is_file():
        parser.error('Local Google Chrome missing')
    # Never attach to another browser, and require the existing direct transport.
    with socket.socket() as s:
        s.bind(('127.0.0.1', 9222))
    with socket.create_connection(('127.0.0.1', 18081), timeout=2):
        pass
    output = ROOT / 'results'
    output.mkdir(exist_ok=True)
    claim = output / 'attempt.claim'
    if claim.exists():
        parser.error('Attempt already claimed; no retry')
    private = ROOT / 'private'
    private.mkdir(exist_ok=True)
    report = dict(started_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                  manual_control='not_performed_user_requested_linux_trial',
                  profile='fresh_dedicated_linux', transport='existing_direct_CONNECT_eno1_mark_0x2024',
                  outcome='environment_error', goto_calls=0, documents=[], items=[])
    process = None
    try:
        process = subprocess.Popen([
            'xvfb-run', '-a', '-s', '-screen 0 1440x1000x24 -nolisten tcp',
            str(CHROME), f'--user-data-dir={private / "profile"}',
            '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=9222',
            '--proxy-server=http://127.0.0.1:18081',
            '--no-first-run', '--no-default-browser-check', 'about:blank'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        # Poll only the local debugging endpoint, never Avito.
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError('chrome_start_failed')
            try:
                report['endpoint'] = helpers.endpoint_check()
                break
            except Exception:
                time.sleep(0.2)
        else:
            raise RuntimeError('cdp_start_timeout')
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(helpers.ENDPOINT, no_defaults=True, timeout=10000)
            report['chrome_version'] = browser.version
            if len(browser.contexts) != 1:
                raise RuntimeError('unexpected_contexts')
            context = browser.contexts[0]
            pages = [p for p in context.pages if p.url == 'about:blank']
            if len(pages) != 1:
                raise RuntimeError('unexpected_tabs')
            page = pages[0]
            report['before_navigation'] = helpers.snapshot(page)

            def record(response):
                if response.request.is_navigation_request() and response.frame == page.main_frame:
                    report['documents'].append(dict(status=response.status, url=helpers.safe_url(response.url)))

            page.on('response', record)
            # Claim only once the local environment is ready, before any Avito navigation.
            claim.touch(exist_ok=False)
            report['goto_calls'] = 1
            try:
                response = page.goto(helpers.SEARCH, wait_until='domcontentloaded', timeout=30000)
                report['http_status'] = response.status if response else None
                state = helpers.snapshot(page)
                if report['http_status'] not in (403, 429, 439) and not state['block_markers']:
                    page.wait_for_function('''markers => {
                      const text = (document.body?.innerText || '').toLowerCase();
                      return markers.some(m => text.includes(m)) || document.querySelector(
                        '[data-marker="catalog-serp"] [data-marker="item"] [data-marker="item-title"]');
                    }''', arg=helpers.BLOCKS, timeout=15000)
            except Exception as exc:
                report['navigation_error_type'] = type(exc).__name__
            report['final_url'] = helpers.safe_url(page.url)
            report['after_navigation'] = helpers.snapshot(page)
            blocked = bool(report['after_navigation']['block_markers']) or any(
                d['status'] in (403, 429, 439) for d in report['documents'])
            if blocked:
                report['outcome'] = 'blocked'
            else:
                report['items'] = helpers.extract_rows(page)
                report['outcome'] = ('success' if report.get('http_status') == 200
                                     and page.url == helpers.SEARCH and report['items']
                                     and not report.get('navigation_error_type') else 'inconclusive')
            browser.close()
    except Exception as exc:
        report['error_type'] = type(exc).__name__
    finally:
        if process is not None and process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        report['result_count'] = len(report['items'])
        report['finished_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
        (output / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['outcome'] == 'success' else 2


if __name__ == '__main__':
    raise SystemExit(main())
