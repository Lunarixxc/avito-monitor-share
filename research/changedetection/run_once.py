"""Offline fixture by default; --live authorizes exactly one Avito fetch."""
import argparse
import datetime as dt
from functools import partial
import http.server
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import threading
import time
import urllib.request

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent.parent
IMAGE = 'ghcr.io/dgtlmoon/changedetection.io@sha256:096dae27b5d677b89f0e810fff95a70403271aa3ff3b6437952d2db9be7e74c5'
CONTAINER = 'avito-changedetection-once'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    mode = 'live' if args.live else 'offline'
    output = ROOT / 'results' / mode
    output.mkdir(parents=True, exist_ok=True)
    if args.live and (output / 'attempt.claim').exists():
        parser.error('Live attempt already claimed; no repeat allowed')
    if args.live:
        preflight = json.loads((ROOT / 'results/offline/result.json').read_text())
        if preflight['outcome'] != 'catalog_available' or preflight['goto_calls'] != 1:
            parser.error('Successful offline preflight required')
    for port in (9225, 18081):
        with socket.socket() as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(('127.0.0.1', port))
    subprocess.run(['docker', 'image', 'inspect', IMAGE], check=True, stdout=subprocess.DEVNULL)
    profile = ROOT / 'private' / (mode + '-profile')
    if args.live and not profile.exists():
        shutil.copytree(PROJECT / 'research/cdp-linux/private/manual-profile', profile,
                        ignore=shutil.ignore_patterns('Singleton*'), symlinks=True)
    else:
        profile.mkdir(parents=True, exist_ok=True)
    chrome = server = None
    transport_started = False
    meta = {'started_at': dt.datetime.now(dt.timezone.utc).isoformat(),
            'image': IMAGE, 'mode': mode, 'browser': '/opt/google/chrome/chrome',
            'profile': 'copy_of_saved_manual_profile' if args.live else 'offline_only'}
    try:
        if args.live:
            (output / 'attempt.claim').touch(exist_ok=False)
            url = 'https://www.avito.ru/kazan/noutbuki'
        else:
            fixture = output / 'fixture'
            fixture.mkdir(exist_ok=True)
            (fixture / 'index.html').write_text('<div data-marker="catalog-serp">'
                '<div data-marker="item" data-item-id="123">'
                '<a data-marker="item-title">Offline laptop</a>'
                '<meta itemprop="price" content="40000"></div></div>')
            class QuietHandler(http.server.SimpleHTTPRequestHandler):
                def log_message(self, *args):
                    pass
            server = http.server.ThreadingHTTPServer(('127.0.0.1', 0),
                partial(QuietHandler, directory=str(fixture)))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            url = f'http://127.0.0.1:{server.server_port}/'
        chrome = subprocess.Popen(['xvfb-run', '-a', '-s',
            '-screen 0 1440x1000x24 -nolisten tcp', '/opt/google/chrome/chrome',
            f'--user-data-dir={profile}', '--remote-debugging-address=127.0.0.1',
            '--remote-debugging-port=9225', '--proxy-server=http://127.0.0.1:18081',
            '--disable-background-networking', '--disable-quic', '--no-first-run',
            '--no-default-browser-check', 'about:blank'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for _ in range(100):
            try:
                with opener.open('http://127.0.0.1:9225/json/version', timeout=1) as r:
                    endpoint = json.load(r)
                break
            except OSError:
                if chrome.poll() is not None:
                    raise RuntimeError('Chrome exited')
                time.sleep(0.1)
        else:
            raise RuntimeError('Chrome CDP not ready')
        meta['chrome_version'] = endpoint['Browser']
        if args.live:
            transport_started = True
            subprocess.run(['bash', str(PROJECT / 'start-direct.sh')], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cmd = ['docker', 'run', '--rm', '--name', CONTAINER, '--network', 'host',
            '--user', f'{os.getuid()}:{os.getgid()}', '--cap-drop', 'ALL',
            '--security-opt', 'no-new-privileges', '--entrypoint', 'python',
            '-v', f'{ROOT}:/work:ro', '-v', f'{ROOT / "results"}:/work/results:rw']
        env = {'PYTHONPATH':'/app:/usr/local', 'TEST_MODE':mode, 'TEST_URL':url, 'BROWSER_WS':endpoint['webSocketDebuggerUrl'],
            'PLAYWRIGHT_DRIVER_URL':endpoint['webSocketDebuggerUrl'],
            'FAST_PUPPETEER_CHROME_FETCHER':'true', 'LOGURU_AUTOINIT':'False',
            'WEBDRIVER_DELAY_BEFORE_CONTENT_READY':'15',
            'PUPPETEER_MAX_PROCESSING_TIMEOUT_SECONDS':'90',
            'SCREENSHOT_MAX_HEIGHT':'1000', 'PYTHONDONTWRITEBYTECODE':'1'}
        for key, value in env.items():
            cmd.extend(['-e', f'{key}={value}'])
        result = subprocess.run(cmd + [IMAGE, '/work/fetch_once.py'], timeout=120,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        meta['container_exit_code'] = result.returncode
    finally:
        subprocess.run(['docker', 'stop', '-t', '5', CONTAINER],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if chrome is not None:
            try:
                os.killpg(chrome.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                chrome.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(chrome.pid, signal.SIGKILL)
                chrome.wait()
        if transport_started:
            subprocess.run(['sudo', '-n', 'systemctl', 'stop', 'avito-research-direct-v2.service'], check=True)
        if server:
            server.shutdown()
        meta['finished_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
        (output / 'run.json').write_text(json.dumps(meta, indent=2))
    report = json.loads((output / 'result.json').read_text())
    print(json.dumps({'mode':mode, 'outcome':report['outcome'],
                     'http_status':report['http_status'], 'goto_calls':report['goto_calls'],
                     'count':report.get('page', {}).get('count')}, ensure_ascii=False))
    return 0 if report['outcome'] == 'catalog_available' else 2


if __name__ == '__main__':
    raise SystemExit(main())
