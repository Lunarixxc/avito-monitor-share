"""One Chrome startup navigation, delayed CDP read, no goto/reload or retries."""
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
from urllib.parse import urldefrag

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('helpers', ROOT.parent / 'cdp-windows/probe.py')
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-once', action='store_true')
    args = parser.parse_args()
    if not args.run_once:
        parser.error('--run-once required')
    os.umask(0o077)
    previous = json.loads((ROOT / 'results/manual-navigation/result.json').read_text())
    elapsed = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(previous['finished_at'])).total_seconds()
    if elapsed < 3600:
        parser.error('Minimum pause has not elapsed')
    profile = ROOT / 'private/manual-profile'
    if not (profile / 'Default').is_dir():
        parser.error('Saved manual profile missing')
    with socket.socket() as s:
        s.bind(('127.0.0.1', 9223))
    with socket.create_connection(('127.0.0.1', 18081), timeout=2):
        pass
    output = ROOT / 'results/startup-once'
    output.mkdir(parents=True, exist_ok=True)
    try:
        (output / 'attempt.claim').touch(exist_ok=False)
    except FileExistsError:
        parser.error('Attempt already claimed; no browser started')
    report = dict(started_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                  pause_since_last_recorded_attempt_seconds=round(elapsed),
                  profile='saved_manual_profile', startup_url=helpers.SEARCH,
                  pre_attach_wait_seconds=20, goto_calls=0, reload_calls=0,
                  startup_navigation_count=0, outcome='environment_error', items=[],
                  initial_http_status=None,
                  initial_http_note='No CDP during startup; initial response chain was not observed')
    process = None
    try:
        report['startup_navigation_count'] = 1
        process = subprocess.Popen([
            'xvfb-run', '-a', '-s', '-screen 0 1440x1000x24 -nolisten tcp',
            '/opt/google/chrome/chrome', f'--user-data-dir={profile}',
            '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=9223',
            '--proxy-server=http://127.0.0.1:18081', '--no-first-run',
            '--no-default-browser-check', helpers.SEARCH],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        time.sleep(20)
        if process.poll() is not None:
            raise RuntimeError('Chrome exited before attachment')
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp('http://127.0.0.1:9223', no_defaults=True, timeout=10000)
            report['chrome_version'] = browser.version
            if len(browser.contexts) != 1:
                raise RuntimeError('Unexpected contexts')
            pages = browser.contexts[0].pages
            report['page_count'] = len(pages)
            candidates = [p for p in pages if urldefrag(p.url).url == helpers.SEARCH]
            if len(candidates) != 1:
                report['outcome'] = 'expected_page_not_unique'
            else:
                page = candidates[0]
                report['final_url'] = helpers.safe_url(page.url)
                report['after_attach'] = helpers.snapshot(page)
                report['navigation_timing'] = page.evaluate('''() => {
                  const n = performance.getEntriesByType('navigation')[0];
                  return n ? {type:n.type, response_status:n.responseStatus || null,
                    transfer_size:n.transferSize, duration_ms:Math.round(n.duration)} : null;
                }''')
                if report['after_attach']['block_markers']:
                    report['outcome'] = 'blocked'
                else:
                    report['items'] = helpers.extract_rows(page)
                    report['outcome'] = 'catalog_available' if report['items'] else 'inconclusive'
            # Graceful shutdown of our own Chrome; no further navigation.
            session = browser.new_browser_cdp_session()
            try:
                session.send('Browser.close')
            except Exception:
                pass  # Target may disconnect before acknowledging close.
            # Let Chrome flush profile state before the fallback group termination.
            try:
                process.wait(timeout=15)
                report['graceful_process_exit'] = True
            except subprocess.TimeoutExpired:
                report['graceful_process_exit'] = False
    except Exception as exc:
        report['error_type'] = type(exc).__name__
    finally:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        report['result_count'] = len(report['items'])
        report['finished_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
        (output / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'outcome':report['outcome'], 'result_count':report['result_count'],
                          'report':str(output / 'result.json')}, ensure_ascii=False))
    return 0 if report['outcome'] == 'catalog_available' else 2


if __name__ == '__main__':
    raise SystemExit(main())
