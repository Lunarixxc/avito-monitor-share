"""Read an already manually opened catalog; never navigate or close Chrome."""
import argparse
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
from urllib.parse import urldefrag

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('helpers', ROOT.parent / 'cdp-windows/probe.py')
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)


def read_page(page):
    state = helpers.snapshot(page)
    return dict(state=state, navigation=page.evaluate('''() => {
      const n = performance.getEntriesByType('navigation')[0];
      return {fragment_present: !!location.hash, referrer_present: !!document.referrer,
        history_length: history.length, timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
        navigation: n ? {type:n.type, status:n.responseStatus || null,
          protocol:n.nextHopProtocol, redirects:n.redirectCount,
          transfer_size:n.transferSize, encoded_size:n.encodedBodySize,
          decoded_size:n.decodedBodySize, duration_ms:Math.round(n.duration),
          response_start_ms:Math.round(n.responseStart),
          dom_complete_ms:Math.round(n.domComplete),
          start_utc:new Date(performance.timeOrigin+n.startTime).toISOString()} : null};
    }'''), items=[] if state['block_markers'] else helpers.extract_rows(page))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manual-ready', action='store_true')
    args = parser.parse_args()
    if not args.manual_ready:
        parser.error('Confirm the manual page is ready with --manual-ready')
    os.umask(0o077)
    output = ROOT / 'results/manual-observation'
    output.mkdir(parents=True, exist_ok=True)
    try:
        (output / 'read.claim').touch(exist_ok=False)
    except FileExistsError:
        parser.error('Already read; no connection made')
    report = dict(started_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                  goto_calls=0, reload_calls=0, outcome='error',
                  limitation='Only the current document timing is available; prior responses were not observed')
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp('http://127.0.0.1:9223',
                                                   no_defaults=True, timeout=10000)
            pages = [p for c in browser.contexts for p in c.pages
                     if urldefrag(p.url).url == helpers.SEARCH]
            if len(pages) != 1:
                raise RuntimeError('Expected one search tab')
            report['chrome_version'] = browser.version
            report.update(read_page(pages[0]))
            report['outcome'] = ('blocked' if report['state']['block_markers'] else
                                 'catalog_available' if report['items'] else 'inconclusive')
            # Driver disconnect only; owner retains the browser and loaded tab.
    except Exception as exc:
        report['error_type'] = type(exc).__name__
    finally:
        report['finished_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
        (output / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps(dict(outcome=report['outcome'], report=str(output / 'result.json'))))
    return 0 if report['outcome'] == 'catalog_available' else 2


if __name__ == '__main__':
    raise SystemExit(main())
