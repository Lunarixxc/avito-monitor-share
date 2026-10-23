"""One diagnostic request after 24h; default mode is a read-only dry run."""
import argparse
import datetime as dt
import fcntl
import http.client
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import time

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent.parent
RESEARCH = PROJECT / 'research/parser-avito'
BASELINE = dt.datetime.fromisoformat('2026-09-19T16:06:11.708067+00:00')
MIN_PAUSE = dt.timedelta(hours=24)


def direct_ip():
    host = 'api.ipify.org'
    address = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)[0][4]
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(20)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_MARK, 0x2024)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, b'eno1\0')
        sock.connect(address)
        with ssl.create_default_context().wrap_socket(sock, server_hostname=host) as tls:
            tls.sendall(b'GET /?format=json HTTP/1.1\r\nHost: api.ipify.org\r\nConnection: close\r\n\r\n')
            response = http.client.HTTPResponse(tls)
            response.begin()
            if response.status != 200:
                raise RuntimeError('IP measurement failed')
            ip = json.loads(response.read(4096))['ip']
    return {'public_ip': ip, 'interface': 'eno1', 'socket_mark': '0x2024',
            'checked_at': dt.datetime.now(dt.timezone.utc).isoformat()}


def latest_request():
    latest = BASELINE
    paths = list((PROJECT / 'results').glob('*/result.json'))
    paths += list((RESEARCH / 'evidence').glob('*/summary.json'))
    for path in paths:
        data = json.loads(path.read_text())
        if data.get('offline') or not data.get('checked_at'):
            continue
        when = dt.datetime.fromisoformat(data['checked_at'])
        if when.tzinfo is None:
            raise ValueError('Timestamp without timezone')
        latest = max(latest, when)
    return latest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--execute', action='store_true')
    action.add_argument('--ip-only', action='store_true')
    args = parser.parse_args()
    if args.ip_only:
        print(json.dumps(direct_ip()))
        return
    os.umask(0o077)
    now = dt.datetime.now(dt.timezone.utc)
    last = latest_request()
    report = {'checked_at': now.isoformat(), 'last_known_request': last.isoformat(),
              'earliest_check': (last + MIN_PAUSE).isoformat(),
              'quiet_hours': round((now - last).total_seconds() / 3600, 3),
              'avito_probe_started': False, 'status': 'dry_run'}
    if not args.execute:
        print(json.dumps(report, indent=2))
        return
    process = None
    with (PROJECT / 'private/probe.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            if dt.datetime.now(dt.timezone.utc) - latest_request() < MIN_PAUSE:
                report['status'] = 'skipped_insufficient_pause'
                return
            # Exclusive claim prevents accidental repeated service starts.
            with (ROOT / 'attempt.claim').open('x') as claim:
                claim.write(now.isoformat())
            report['exit_measurement'] = direct_ip()
            report['same_ip_as_baseline'] = report['exit_measurement']['public_ip'] == '178.205.51.86'
            cookies = RESEARCH / 'evidence/profile-cookies.private.json'
            cache = RESEARCH / 'evidence/2026-09-19T15-57-02.158081+00-00/api_url_private.json'
            if not json.loads(cookies.read_text()).get('cookies'):
                raise ValueError('Cookie fixture missing')
            cached = json.loads(cache.read_text())
            if 'https://www.avito.ru/kazan/noutbuki' not in cached:
                raise ValueError('API cache missing; no SPFA calls permitted')
            with socket.socket() as check:
                check.settimeout(1)
                if check.connect_ex(('127.0.0.1', 18081)) == 0:
                    raise RuntimeError('Direct transport already in use')
            with (ROOT / 'transport.log').open('x') as log:
                process = subprocess.Popen(['/usr/bin/python3', str(PROJECT / 'direct_proxy.py')],
                                           stdout=log, stderr=subprocess.STDOUT)
                for _ in range(30):
                    if process.poll() is not None:
                        raise RuntimeError('Direct transport failed to start')
                    with socket.socket() as check:
                        check.settimeout(0.1)
                        if check.connect_ex(('127.0.0.1', 18081)) == 0:
                            break
                    time.sleep(0.1)
                else:
                    raise RuntimeError('Direct transport not ready')
                report['avito_probe_started'] = True
                result = subprocess.run([
                    str(RESEARCH / '.venv/bin/python'), str(RESEARCH / 'probe_http.py'),
                    '--live', '--cookies-file', str(cookies), '--api-cache', str(cache)],
                    capture_output=True, text=True, timeout=45)
                (ROOT / 'probe-output.txt').write_text(result.stdout)
                report['probe_returncode'] = result.returncode
                report['status'] = 'probe_finished_review_evidence'
        except Exception as exc:
            report.update(status='diagnostic_error', error_type=type(exc).__name__)
        finally:
            if process:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            (ROOT / 'cooldown-result.json').write_text(json.dumps(report, indent=2))
            print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
