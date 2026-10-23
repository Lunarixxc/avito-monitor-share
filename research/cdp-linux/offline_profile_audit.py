"""Audit a disposable profile copy with all browser destinations denied."""
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import sqlite3
import subprocess
import tempfile
import time
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / 'private/manual-profile'


def metadata(profile):
    prefs = json.loads((profile / 'Default/Preferences').read_text())
    db = profile / 'Default/Cookies'
    if not db.exists():
        db = profile / 'Default/Network/Cookies'
    with sqlite3.connect(db.as_uri() + '?mode=ro', uri=True) as conn:
        integrity = conn.execute('PRAGMA quick_check').fetchone()[0]
        rows = conn.execute('''SELECT name,is_persistent,expires_utc FROM cookies
            WHERE host_key='avito.ru' OR host_key LIKE '%.avito.ru' ''').fetchall()
    now = (dt.datetime.now(dt.timezone.utc) - dt.datetime(1601,1,1,tzinfo=dt.timezone.utc)).total_seconds()*1e6
    return dict(exit_type=prefs.get('profile', {}).get('exit_type'), integrity=integrity,
                cookie_count=len(rows), unexpired_count=sum(not e or e>now for _,_,e in rows),
                persistent_count=sum(p for _,p,_ in rows), cookie_names=sorted({n for n,_,_ in rows}))


def main():
    os.umask(0o077)
    with socket.socket() as check:
        check.bind(('127.0.0.1', 9224))
    report = dict(checked_at=dt.datetime.now(dt.timezone.utc).isoformat(), source=metadata(SOURCE))
    # Reserve a loopback socket without listening: all proxy connection attempts fail.
    # No external DNS: host-resolver-rules maps all nonliteral hosts to NOTFOUND.
    with socket.socket() as denied, tempfile.TemporaryDirectory(prefix='offline-audit-', dir=ROOT/'private') as temp:
        denied.bind(('127.0.0.1', 0))
        proxy_port = denied.getsockname()[1]
        profile = Path(temp)/'profile'
        shutil.copytree(SOURCE, profile, ignore=shutil.ignore_patterns('Singleton*'), symlinks=True)
        for suffix in ('', '1'):
            process = subprocess.Popen(['xvfb-run','-a','-s','-screen 0 1440x1000x24 -nolisten tcp',
                '/opt/google/chrome/chrome', f'--user-data-dir={profile}',
                '--remote-debugging-address=127.0.0.1','--remote-debugging-port=9224',
                f'--proxy-server=http://127.0.0.1:{proxy_port}', '--host-resolver-rules=MAP * ~NOTFOUND',
                '--disable-quic','--disable-background-networking','--disable-component-update',
                '--no-first-run','--no-default-browser-check','about:blank'],
                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
            try:
                time.sleep(3)
                with sync_playwright() as pw:
                    browser=pw.chromium.connect_over_cdp('http://127.0.0.1:9224',no_defaults=True,timeout=5000)
                    cookies=browser.contexts[0].cookies('https://www.avito.ru/')
                    report['loaded'+suffix]={'count':len(cookies),'names':sorted({c['name'] for c in cookies})}
                    del cookies  # Values never serialized or logged.
                    session=browser.new_browser_cdp_session()
                    try:
                        session.send('Browser.close')
                    except Exception:
                        pass
                    # Unlike the live runner, wait for shutdown BEFORE signalling its group.
                    try:
                        process.wait(timeout=15)
                        report['graceful_exit'+suffix]=True
                    except subprocess.TimeoutExpired:
                        report['graceful_exit'+suffix]=False
                report['after_close'+suffix]=metadata(profile)
            finally:
                try:
                    os.killpg(process.pid,signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid,signal.SIGKILL)
                    process.wait()
    report['source_after']=metadata(SOURCE)
    output=ROOT/'results/offline-profile-audit.json'
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
