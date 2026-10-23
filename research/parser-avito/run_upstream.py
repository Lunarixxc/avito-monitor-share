"""Run the actual upstream application once; config check is offline by default."""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent


def read_config():
    values = dotenv_values(ROOT / '.env')
    key = os.environ.get('SPFA_API_KEY', values.get('SPFA_API_KEY')) or ''
    proxy = os.environ.get('AVITO_PROXY', values.get('AVITO_PROXY')) or ''
    url = os.environ.get('AVITO_SEARCH_URL', values.get('AVITO_SEARCH_URL')) or 'https://www.avito.ru/kazan/noutbuki'
    issues = []
    if not key.strip():
        issues.append('SPFA_API_KEY is missing')
    try:
        parts = urlsplit('http://' + proxy)
        if (not parts.username or not parts.password or not parts.hostname or not parts.port
                or parts.path or parts.query or parts.fragment or '://' in proxy):
            issues.append('AVITO_PROXY must be login:password@host:port, without scheme')
    except ValueError:
        issues.append('AVITO_PROXY has invalid syntax')
    try:
        search = urlsplit(url)
        valid_search = (search.scheme == 'https' and search.hostname == 'www.avito.ru'
                        and not search.username and not search.password
                        and search.port in (None, 443))
    except ValueError:
        valid_search = False
    if not valid_search:
        issues.append('AVITO_SEARCH_URL must use https://www.avito.ru')
    return key, proxy, url, issues


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true', help='Fetch one search page')
    parser.add_argument('--allow-cookie-purchase', action='store_true', help='Allow at most one SPFA cookie purchase in this process')
    args = parser.parse_args()
    key, proxy, url, issues = read_config()
    if not args.once or issues:
        print(json.dumps({'mode': 'offline_config_check', 'ready': not issues,
                          'issues': issues, 'network_requests': 0}, ensure_ascii=False, indent=2))
        return 2 if issues else 0

    os.umask(0o077)
    runtime = ROOT / 'runtime'
    runtime.mkdir(mode=0o700, exist_ok=True)
    import fcntl
    lock = (runtime / 'run.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.chdir(runtime)
    sys.path.insert(0, str(ROOT / 'upstream'))
    from loguru import logger
    logger.remove()
    import parser_cls
    logger.remove()  # Upstream import adds its own file sink; no secrets in logs.
    from dto import AvitoConfig
    from parser.cookies.external_api import ExternalApiCookiesProvider

    class BoundedCookies(ExternalApiCookiesProvider):
        purchases = 0

        def _get_new_cookies(self):
            if not args.allow_cookie_purchase or self.purchases >= 1:
                raise RuntimeError('cookie_purchase_not_authorized_or_limit_reached')
            self.purchases += 1
            return super()._get_new_cookies()

    # Keep upstream composition; only bound spending during this diagnostic run.
    parser_cls.build_cookies_provider = lambda config, proxy: BoundedCookies(config, proxy=proxy)
    config = AvitoConfig(urls=[url], proxy_string=proxy, cookies_api_key=key,
                         use_bypass_api=True, count=1, one_time_start=True,
                         max_count_of_retry=1, block_threshold=2, retry_delay=0,
                         timeout=45, pause_between_links=0, save_xlsx=True,
                         parse_phone=False, parse_views=False)
    app = None
    report = {'mode': 'actual_upstream_once', 'pages_requested': 1}
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            app = parser_cls.AvitoParse(config)
            app.parse()
        report.update(good_requests=app.good_request_count, bad_requests=app.bad_request_count,
                      cookie_purchase_attempts=app.cookies_provider.purchases)
        report['status'] = 'request_completed_check_export' if app.good_request_count else 'no_successful_request'
    except Exception as exc:
        report.update(status='failed', error_type=type(exc).__name__)
    finally:
        if app:
            app.http._client.close()
        lock.close()
    (runtime / 'last_run.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get('good_requests', 0) > 0 else 2


if __name__ == '__main__':
    raise SystemExit(main())
