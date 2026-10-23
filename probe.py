"""Bounded live-page research; preserve evidence and never retry automatically."""
import argparse
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import select
import signal
import sys
import time

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
RESTRICTED = {'IP_BLOCKED', 'CAPTCHA', 'access_restricted'}


def write_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temporary.replace(path)


def classify(body, status=None):
    text = body.casefold()
    if 'проблема с ip' in text:
        return 'IP_BLOCKED'
    if any(s in text for s in ('подтвердите, что вы не робот', 'проверка браузера')):
        return 'CAPTCHA'
    if status in (403, 429) or any(s in text for s in ('доступ ограничен', 'слишком много запросов')):
        return 'access_restricted'
    return None


def capture(page, result, output, private):
    """Save metadata even if one browser operation (e.g. screenshot) fails."""
    errors = {}
    if page and not page.is_closed():
        result['final_url'] = page.url
        try:
            body = page.locator('body').inner_text(timeout=3000)
            (output / 'page.txt').write_text(body)
            restricted = classify(body, result.get('http_status'))
            if restricted:
                result.update(status=restricted, access_restricted=True)
            elif result['status'] not in RESTRICTED:
                result['candidate_count'] = page.locator('[data-marker="catalog-serp"] [data-marker="item"]').count()
                result['status'] = 'page_captured' if result['candidate_count'] else 'extraction_error'
            result['title'] = page.title()
        except Exception as exc:
            errors['text'] = type(exc).__name__
        try:
            (output / 'page.html').write_text(page.content())
        except Exception as exc:
            errors['html'] = type(exc).__name__
        # Persist the status before attempting a screenshot.
        write_json(output / 'result.json', result)
        try:
            page.screenshot(path=str(output / 'page.png'), full_page=True, timeout=5000)
        except Exception as exc:
            errors['screenshot'] = type(exc).__name__
    else:
        errors['page'] = 'closed_or_unavailable'
    if errors:
        result['capture_errors'] = errors
    if result['status'] in RESTRICTED:
        blocked = private / 'blocked.json'
        old = json.loads(blocked.read_text()) if blocked.exists() else {}
        # Do not replace specific IP evidence with a later generic HTTP failure.
        if old.get('status') != 'IP_BLOCKED' or result['status'] == 'IP_BLOCKED':
            write_json(blocked, {'status': result['status'], 'checked_at': result['checked_at'], 'result': str(output)})
    write_json(output / 'result.json', result)


def wait_manual(page, seconds, stopped):
    """Read the foreground TTY without starving Playwright's event loop."""
    print('Press Enter IN THIS SSH TERMINAL to save and close; Ctrl+C also saves.', flush=True)
    deadline = time.monotonic() + seconds
    while not stopped:
        if page.is_closed():
            return 'browser_closed'
        if time.monotonic() >= deadline:
            return 'manual_timeout'
        if select.select([sys.stdin], [], [], 0)[0]:
            data = os.read(sys.stdin.fileno(), 4096)
            if not data:
                return 'stdin_eof'
            if b'\n' in data or b'\r' in data:
                return 'enter'
        page.wait_for_timeout(100)
    return stopped[0]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='https://www.avito.ru/kazan/noutbuki')
    parser.add_argument('--headed', action='store_true')
    parser.add_argument('--manual', action='store_true')
    parser.add_argument('--manual-timeout', type=float, default=3600)
    parser.add_argument('--state-dir', type=Path, default=ROOT)
    parser.add_argument('--offline-html', type=Path, help='Local fixture; browser context offline, no page request')
    args = parser.parse_args()
    os.umask(0o077)
    private = args.state_dir.resolve() / 'private'
    private.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = (private / 'probe.lock').open('w')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if (private / 'blocked.json').exists() and not args.manual:
        raise SystemExit('Collection paused: inspect saved evidence; no request made.')
    started = time.monotonic()
    stamp = dt.datetime.now(dt.timezone.utc).isoformat()
    output = args.state_dir.resolve() / 'results' / stamp.replace(':', '-')
    output.mkdir(parents=True)
    result = dict(checked_at=stamp, url=args.url if not args.offline_html else 'offline fixture',
                  status='environment_error', items=[], access_restricted=False,
                  profile_preexisting=(private / 'chromium').exists(),
                  offline=bool(args.offline_html), source_verified=False)
    stopped = []
    def stop(signum, frame):
        if not stopped:
            stopped.append(signal.Signals(signum).name)
    old_handlers = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    context = page = None
    try:
        with sync_playwright() as pw:
            try:
                context = pw.chromium.launch_persistent_context(
                    str(private / 'chromium'), headless=not args.headed,
                    proxy={'server': 'http://127.0.0.1:18081'},
                    locale='ru-RU', timezone_id='Europe/Moscow',
                    viewport={'width': 1440, 'height': 1000},
                    offline=bool(args.offline_html), service_workers='block',
                    args=['--disable-quic'])
                page = context.pages[0] if context.pages else context.new_page()
                # Keep the last MAIN document status, including human navigation.
                def response_seen(response):
                    if response.request.is_navigation_request() and response.frame == page.main_frame:
                        result['http_status'] = response.status
                        if response.status in (403, 429):
                            result.update(status='access_restricted', access_restricted=True)
                            if not (private / 'blocked.json').exists():
                                write_json(private / 'blocked.json', {'status': 'access_restricted', 'checked_at': stamp, 'result': str(output)})
                page.on('response', response_seen)
                result['status'] = 'network_error'
                try:
                    if args.offline_html:
                        page.set_content(args.offline_html.read_text(), wait_until='domcontentloaded')
                    else:
                        page.goto(args.url, wait_until='domcontentloaded', timeout=45000)
                except Exception as exc:
                    result['navigation_error'] = type(exc).__name__
                capture(page, result, output, private)
                if args.manual and not stopped:
                    result['finish_reason'] = wait_manual(page, args.manual_timeout, stopped)
                else:
                    result['finish_reason'] = stopped[0] if stopped else 'capture_complete'
            except Exception as exc:
                result['error_type'] = type(exc).__name__
            finally:
                capture(page, result, output, private)
                if context:
                    context.close()
    except Exception as exc:
        result['error_type'] = type(exc).__name__
    finally:
        result.setdefault('finish_reason', stopped[0] if stopped else 'error')
        result['duration_seconds'] = round(time.monotonic() - started, 2)
        write_json(output / 'result.json', result)
        for sig, previous in old_handlers.items():
            signal.signal(sig, previous)
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
        print('Evidence:', output, flush=True)
    return 0 if result['status'] == 'page_captured' else 2


if __name__ == '__main__':
    raise SystemExit(main())
