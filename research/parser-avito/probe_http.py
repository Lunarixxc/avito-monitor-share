"""Explicit one-shot experiment; no purchased cookies, browser or retries."""
import argparse
import contextlib
import datetime as dt
import io
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'upstream'))
from loguru import logger
from parser.http.client import HttpClient
from parser.proxies.proxy import ServerProxy
from parser.url_converter import AvitoUrlConverter
from parser.cookies.own_cookies import OwnCookiesProvider
from check_offline import upstream_method


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', required=True)
    parser.add_argument('--cookies-file', type=Path)
    parser.add_argument('--api-cache', type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    logger.remove()
    stamp = dt.datetime.now(dt.timezone.utc).isoformat()
    output = ROOT / 'evidence' / stamp.replace(':', '-')
    output.mkdir(parents=True)
    report = {'checked_at': stamp, 'source_url': 'https://www.avito.ru/kazan/noutbuki',
              'cookies': 'none; existing browser profile untouched',
              'avito_proxy': 'http://127.0.0.1:18081', 'max_avito_attempts': 1}
    cache = output / 'api_url_private.json'
    if args.api_cache:
        cache.write_bytes(args.api_cache.read_bytes())
    converter = AvitoUrlConverter(cache_path=cache)
    import requests
    original_post = requests.post

    def observed_post(*a, **kw):
        kw['allow_redirects'] = False
        response = original_post(*a, **kw)
        report['converter_http_status'] = response.status_code
        report['converter_content_type'] = response.headers.get('content-type')
        return response

    target = report['source_url']
    report['avito_mode'] = 'HTML fallback diagnostic'
    try:
        with patch('parser.url_converter.requests.post', side_effect=observed_post):
            converted = converter.convert(target)
        parts = urlsplit(converted)
        if (parts.scheme != 'https' or parts.port not in (None, 443) or parts.username
                or not parts.hostname or not (parts.hostname == 'avito.ru' or parts.hostname.endswith('.avito.ru'))):
            raise ValueError('Unexpected API destination')
        target = upstream_method('_api_url_for_page')(converted, 1)
        report['avito_mode'] = 'mobile API, page=1'
        report['api_host'] = parts.hostname
        report['api_path'] = parts.path
    except Exception as exc:
        report['converter_error_type'] = type(exc).__name__

    cookies = None
    if args.cookies_file:
        cookie_copy = output / 'own_cookies.private.json'
        cookie_copy.write_bytes(args.cookies_file.read_bytes())
        cookies = OwnCookiesProvider(storage_path=cookie_copy, save_on_exit=False)
        report['cookies'] = 'OwnCookiesProvider; offline export from existing browser profile'
        report['initial_cookie_count'] = len(cookies.get())
    client = HttpClient(ServerProxy('127.0.0.1:18081'), cookies=cookies, max_retries=1,
                        retry_delay=0, block_threshold=2, timeout=25)
    original_request = client._client.request

    def observed_request(*a, **kw):
        response = original_request(*a, **kw)
        report['avito_http_status'] = response.status_code
        report['avito_content_type'] = response.headers.get('content-type')
        report['response_bytes'] = len(response.content)
        report['ip_blocked_marker'] = 'проблема с ip' in response.text.casefold()
        report['security_check_marker'] = 'проверка безопасности' in response.text.casefold()
        report['captcha_marker'] = any(s in response.text.casefold() for s in
                                      ['captcha', 'проверка браузера', 'подтвердите, что вы не робот'])
        (output / 'avito_response.private.txt').write_text(response.text)
        if response.status_code == 200:
            if report['avito_mode'].startswith('mobile API'):
                catalog = upstream_method('_extract_api_catalog')(response.json())
            else:
                catalog = upstream_method('find_json_on_page')(response.text).get('catalog', {})
            report['parsed_items'] = len(catalog.get('items', []))
        return response

    try:
        with patch.object(client._client, 'request', side_effect=observed_request), \
                contextlib.redirect_stdout(io.StringIO()):
            client.request('GET', target, allow_redirects=False)
    except Exception as exc:
        report['avito_error_type'] = type(exc).__name__
    finally:
        client._client.close()
        (output / 'summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print('Evidence:', output)


if __name__ == '__main__':
    main()
