"""Run the installed changedetection.io Puppeteer fetcher once, without scheduler."""
import asyncio
import datetime as dt
import json
import logging
import os
from pathlib import Path

from loguru import logger

logger.remove()
logging.disable(logging.CRITICAL)

from changedetectionio.content_fetchers.puppeteer import fetcher
from changedetectionio import __version__
from pyppeteer.page import Page

REPORT = Path('/work/results') / os.environ['TEST_MODE'] / 'result.json'
report = {
    'started_at': dt.datetime.now(dt.timezone.utc).isoformat(),
    'changedetection_version': __version__,
    'backend': 'changedetectionio.content_fetchers.puppeteer.fetcher',
    'mode': os.environ['TEST_MODE'], 'goto_calls': 0, 'retry_blocked': False,
    'document_statuses': [], 'outcome': 'environment_error',
}
original_goto, original_close = Page.goto, Page.close


async def guarded_goto(page, url, *args, **kwargs):
    if report['goto_calls']:
        report['retry_blocked'] = True
        raise RuntimeError('Second programmatic navigation prohibited')
    if url != os.environ['TEST_URL']:
        raise RuntimeError('Unexpected navigation URL')
    report['goto_calls'] += 1

    def response_seen(response):
        if response.frame == page.mainFrame and response.request.isNavigationRequest:
            report['document_statuses'].append(response.status)

    page.on('response', response_seen)
    return await original_goto(page, url, *args, **kwargs)


async def capture_close(page, *args, **kwargs):
    try:
        report['page'] = await asyncio.wait_for(page.evaluate('''() => {
          const root = document.querySelector('[data-marker="catalog-serp"]');
          const text = document.body?.innerText.toLowerCase() || '';
          return {
            catalog_present: !!root,
            count: root?.querySelectorAll('[data-marker="item"]').length || 0,
            block_markers: ['доступ ограничен', 'проблема с ip', 'captcha',
              'проверка безопасности'].filter(x => text.includes(x)),
            webdriver: navigator.webdriver, language: navigator.language,
            items: Array.from(root?.querySelectorAll('[data-marker="item"]') || [])
              .slice(0, 5).map(n => ({id:n.getAttribute('data-item-id'),
                title:n.querySelector('[data-marker="item-title"]')?.textContent.trim(),
                price:n.querySelector('[itemprop="price"]')?.getAttribute('content')}))
          };
        }'''), 5)
    except Exception as exc:
        report.setdefault('snapshot_error_type', type(exc).__name__)
    return await original_close(page, *args, **kwargs)


async def main():
    Page.goto, Page.close = guarded_goto, capture_close
    obj = fetcher(custom_browser_connection_url=os.environ['BROWSER_WS'],
                  screenshot_format='png')
    try:
        await obj.run(url=os.environ['TEST_URL'], timeout=45,
                      request_headers={}, request_method='GET', fetch_favicon=False,
                      watch_uuid='avito-research-once')
    except Exception as exc:
        report['error_type'] = type(exc).__name__
        report['exception_status'] = getattr(exc, 'status_code', None)
    finally:
        report['http_status'] = obj.status_code
        await obj.quit()
        page = report.get('page', {})
        codes = report['document_statuses']
        if page.get('block_markers') or any(s in (403, 429, 439) for s in codes):
            report['outcome'] = 'blocked'
        elif obj.status_code == 200 and page.get('count', 0) > 0:
            report['outcome'] = 'catalog_available'
        elif report['goto_calls']:
            report['outcome'] = 'inconclusive'
        report['finished_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
        REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['outcome'] == 'catalog_available' else 2


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
