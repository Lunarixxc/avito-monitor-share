"""Export Avito cookies from an offline temporary copy; never print values."""
import argparse
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import shutil
import tempfile

from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    if not args.profile.is_dir():
        raise SystemExit('Profile not found')
    if (args.profile / 'SingletonLock').is_symlink():
        raise SystemExit('Profile has a browser lock; close the browser first')
    with (args.profile.parent / 'probe.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with tempfile.TemporaryDirectory(prefix='avito-cookie-export-') as temporary:
            profile = Path(temporary) / 'chromium'
            shutil.copytree(args.profile, profile, ignore=shutil.ignore_patterns(
                'Singleton*', 'Cache', 'Code Cache', 'GPUCache', 'BrowserMetrics'))
            with sync_playwright() as pw:
                context = pw.chromium.launch_persistent_context(
                    str(profile), headless=True, offline=True, service_workers='block',
                    proxy={'server': 'http://127.0.0.1:9'},
                    args=['--disable-background-networking', '--disable-quic'])
                try:
                    cookies = context.cookies(['https://www.avito.ru/'])
                    ua = context.pages[0].evaluate('navigator.userAgent')
                finally:
                    context.close()
            args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            record = {'cookies': {c['name']: c['value'] for c in cookies},
                      'user_agent': ua, 'cookie_records': cookies,
                      'exported_at': dt.datetime.now(dt.timezone.utc).isoformat()}
            with args.output.open('x') as stream:
                json.dump(record, stream)
            print(json.dumps({'cookie_count': len(cookies), 'offline': True,
                              'output': str(args.output), 'source_profile_modified': False}))


if __name__ == '__main__':
    main()
