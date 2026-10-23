"""Bounded upstream experiment. Default: offline configuration check."""
import argparse
import contextlib
import datetime as dt
import json
import logging
import os
from pathlib import Path
import signal
import sys
import time
from urllib.parse import urlsplit, parse_qs, urlencode

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "upstream"))
import main as upstream
from curl_cffi import requests

OriginalSession = requests.Session
OriginalPageUrl = upstream.page_url
PAGE_NUMBERS = (1,)
REQUEST_LIMIT = 32
PROXY = os.environ.get("AVITO_DIRECT_PROXY", "http://127.0.0.1:18081")
DOMAINS = ("avito.ru", "avito.st", "avitocdn.ru", "geetest.com", "geevisit.com")


def configure(pages=1, scan_page=4):
    global PAGE_NUMBERS
    query = os.environ.get("AVITO_SEARCH_QUERY", "").strip()
    sort = os.environ.get("AVITO_SORT", "104")
    search = {"s": sort}
    if query:
        search["q"] = query
    upstream.CATALOG_PAGE_URL = "https://www.avito.ru/kazan/noutbuki?" + urlencode(search)
    upstream.REFERER = upstream.CATALOG_PAGE_URL
    upstream.ITEMS_QUERY_PARAMETERS = tuple(
        (key, "650400" if key in ("locationId", "features[suggestParams][locationID]") else value)
        for key, value in upstream.ITEMS_QUERY_PARAMETERS
        if key not in ("geoCoords", "context", "query", "s")
    )
    upstream.ITEMS_QUERY_PARAMETERS += (("s", sort),)
    if query:
        upstream.ITEMS_QUERY_PARAMETERS += (("query", query),)
    # Keep three fresh pages and use the fourth request for a persistent sweep.
    PAGE_NUMBERS = (1, 2, 3, max(4, scan_page)) if pages == 4 and not query else tuple(range(1, pages + 1))
    upstream.page_url = lambda page: OriginalPageUrl(PAGE_NUMBERS[page - 1])
    for value in vars(upstream).values():
        if isinstance(value, dict) and "Referer" in value:
            if "avito.ru" in value["Referer"]:
                value["Referer"] = upstream.REFERER
    upstream.PAGES_TO_REQUEST = pages
    upstream.MAX_PROTECTION_TRANSITIONS_PER_PAGE = 3
    upstream.MAX_CONSECUTIVE_GEETEST_FAILURES = 1
    upstream.MAX_QRATOR_RETRIES_PER_REQUEST = 1
    upstream.MAX_TRANSIENT_GET_RETRIES = 0
    # Upstream diagnostics include cookies, challenge values and request bodies.
    logging.disable(logging.CRITICAL)
    upstream.save_http_exchange = lambda *a, **k: ROOT / "results" / "summary.json"
    upstream.save_response_body = lambda *a, **k: ROOT / "results" / "summary.json"
    upstream.save_html_response = upstream.save_response_body


class BoundedSession(OriginalSession):
    events = []
    listings = []
    pages = []
    deadline = 0

    def __init__(self, *args, **kwargs):
        kwargs.update(proxy=PROXY, trust_env=False, timeout=10)
        super().__init__(*args, **kwargs)

    def request(self, method, url, **kwargs):
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if (parsed.scheme != "https" or parsed.port not in (None, 443)
                or parsed.username or parsed.password
                or not any(host == d or host.endswith("." + d) for d in DOMAINS)):
            raise RuntimeError("destination_denied")
        if len(self.events) >= REQUEST_LIMIT or time.monotonic() >= self.deadline:
            raise RuntimeError("request_budget_exhausted")
        # Every hop must be counted and checked, including solver image downloads.
        kwargs.update(proxy=PROXY, allow_redirects=False)
        kwargs["timeout"] = min(kwargs.get("timeout") or 10, 10)
        event = {"method": method.upper(), "host": host, "path": parsed.path}
        self.events.append(event)
        try:
            response = super().request(method, url, **kwargs)
            event["status"] = response.status_code
            # Consume streams here so diagnostics/extraction see the same full body.
            if kwargs.get("stream"):
                response.content = b"".join(response.iter_content())
            event["bytes"] = len(response.content)
            if response.status_code == 200 and parsed.path == "/web/1/js/items":
                body = response.json()
                items = body.get("catalog", {}).get("items")
                if not isinstance(items, list):
                    raise ValueError("missing_catalog_items")
                if isinstance(items, list):
                    self.pages.append({"page": parse_qs(parsed.query).get("p", [None])[0],
                                       "ids": [x["id"] for x in items if isinstance(x, dict) and x.get("id")]})
                    for item in items:
                        if not isinstance(item, dict) or not item.get("id"):
                            continue
                        self.listings.append({key: item.get(key) for key in
                                              ("id", "title", "urlPath", "priceDetailed", "description", "geo")})
            elif response.status_code in (400, 404) and parsed.path == "/web/1/js/items":
                self.pages.append({"page": parse_qs(parsed.query).get("p", [None])[0],
                                   "ids": [], "http_status": response.status_code})
            print(json.dumps(event, ensure_ascii=False), flush=True)
            # Main will call iter_content again for a streamed response.
            if kwargs.get("stream"):
                response.iter_content = lambda **kw: iter((response.content,))
            return response
        except Exception as exc:
            event["error_type"] = type(exc).__name__
            raise


def protection_kind(response):
    """Return an enum only: never persist challenges, headers or HTML."""
    if upstream.is_qrator_challenge_response(response):
        return 'qrator'
    if upstream.is_firewall_captcha_dispatcher_response(response):
        return 'captcha_dispatcher'
    if response.status_code in (429, 439) and upstream.response_has_pow_challenge(response):
        return 'pow'
    if response.status_code == 429 and upstream.is_geetest_firewall_html(response.text):
        return 'geetest'
    from tracking import page_facts
    if response.status_code in (403, 429, 439) or page_facts(response.text, response.status_code)['availability'] == 'blocked':
        return 'unsupported'
    return None


def fetch_card(session, url):
    """Use upstream protection handlers, then repeat the same document GET."""
    diagnostic = {'attempts': [], 'transitions': 0, 'blocked': False}
    response = None
    used_qrator = used_captcha = False
    while True:
        try:
            upstream.set_session_headers(session, upstream.DOCUMENT_REQUEST_HEADERS)
            response = session.get(url, stream=True, timeout=10)
            kind = protection_kind(response)
            diagnostic['attempts'].append({'status': response.status_code, 'protection': kind})
            if kind is None:
                return response, diagnostic
            diagnostic['blocked'] = True
            if kind == 'unsupported':
                diagnostic['stop_reason'] = 'unsupported_protection'
                return response, diagnostic
            if (diagnostic['transitions'] >= 3 or (kind == 'qrator' and used_qrator)
                    or (kind in ('geetest', 'captcha_dispatcher') and used_captcha)):
                diagnostic['stop_reason'] = 'protection_limit'
                return response, diagnostic
            if len(BoundedSession.events) + 4 > REQUEST_LIMIT or time.monotonic() + 15 >= BoundedSession.deadline:
                diagnostic['stop_reason'] = 'request_or_time_budget'
                return response, diagnostic
            diagnostic['transitions'] += 1
            # Some solver dependencies print diagnostics. Suppress their output;
            # sanitized HTTP events are still collected by BoundedSession.
            with open(os.devnull, 'w') as quiet, contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
                if kind == 'qrator':
                    used_qrator = True
                    upstream.run_qrator_cookie_flow(session, qrator_html=response.text,
                                                    page_url=url, context='card')
                else:
                    if kind in ('geetest', 'captcha_dispatcher'):
                        used_captcha = True
                    result = upstream.handle_firewall_response(session, response, context='card')
                    # PoW verifies internally and may omit the optional TTL.
                    if result is None and kind != 'pow':
                        raise RuntimeError('protection_not_verified')
            diagnostic['blocked'] = False
        except Exception as exc:
            diagnostic.update(blocked=True, stop_reason='request_or_protection_error', error_type=type(exc).__name__)
            return response, diagnostic


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--run-id", default="", help="New named experiment; existing results are never overwritten")
    parser.add_argument("--pages", type=int, choices=(1, 2, 3, 4), default=1)
    parser.add_argument("--details", type=int, choices=range(17), default=0)
    parser.add_argument("--prepare-digest", action="store_true", help="Refresh best known candidates first")
    args = parser.parse_args()
    import re
    if args.run_id and not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", args.run_id):
        parser.error("invalid run-id")
    configure(args.pages)
    history_path = ROOT / "results" / "monitor" / "state.json"
    history = json.loads(history_path.read_text()) if history_path.exists() else {}
    scan = history.get('catalog_scan', {})
    if scan.get('catalog') == upstream.CATALOG_PAGE_URL:
        configure(args.pages, scan_page=int(scan.get('next_page', 4)))
    BoundedSession.events = []
    BoundedSession.listings = []
    BoundedSession.pages = []
    if not args.live:
        print(json.dumps({"mode": "offline", "catalog": upstream.CATALOG_PAGE_URL,
                          "pages": args.pages, "requested_pages": PAGE_NUMBERS,
                          "request_limit": REQUEST_LIMIT, "seconds": 150,
                          "proxy": PROXY, "cookies": "fresh session"}, ensure_ascii=False))
        return 0
    os.umask(0o077)
    output = ROOT / "results" / args.run_id if args.run_id else ROOT / "results"
    output.mkdir(exist_ok=True, parents=True, mode=0o700)
    # Persistent claim forbids accidental repeated live execution.
    with (output / "live.claim").open("x") as claim:
        claim.write(dt.datetime.now(dt.timezone.utc).isoformat())
    result = {"started": dt.datetime.now(dt.timezone.utc).isoformat(),
              "catalog": upstream.CATALOG_PAGE_URL, "events": BoundedSession.events,
              "status": "failed", "query": os.environ.get("AVITO_SEARCH_QUERY", "").strip(),
              "search_mode": "text" if os.environ.get("AVITO_SEARCH_QUERY", "").strip() else "category",
              "requested_pages": PAGE_NUMBERS,
              "requested_sort": os.environ.get("AVITO_SORT", "104"), "listings": BoundedSession.listings,
              "pages": BoundedSession.pages, "cards": []}
    sessions = []

    def factory(*args, **kwargs):
        session = BoundedSession(*args, **kwargs)
        sessions.append(session)
        return session

    def expired(*args):
        raise TimeoutError("wall_time_budget")

    requests.Session = factory
    BoundedSession.deadline = time.monotonic() + 150
    signal.signal(signal.SIGALRM, expired)
    signal.alarm(150)
    try:
        flow = upstream.run()
        result["chain"] = list(flow.verification_chain)
        result["listings"] = list({str(x["id"]): x for x in BoundedSession.listings}.values())
        valid_pages = {int(x["page"]) for x in BoundedSession.pages if x.get('http_status', 200) == 200}
        deep_unavailable = (result['search_mode'] == 'category' and len(PAGE_NUMBERS) == 4
                            and valid_pages == {1, 2, 3}
                            and any(int(page['page']) == PAGE_NUMBERS[-1]
                                    and page.get('http_status') in (400, 404) for page in BoundedSession.pages))
        result["status"] = "success" if valid_pages == set(PAGE_NUMBERS) or deep_unavailable else "incomplete"
        if deep_unavailable:
            result['deep_scan_status'] = 'page_unavailable'
        if result["status"] == "success" and args.details:
            from filters import parse_card
            from tracking import review_queue, page_facts
            seen = history.get("seen", {})
            candidates = review_queue(result["listings"], seen, args.details,
                                      int(os.environ.get("AVITO_MAX_PRICE", "65000")), result["started"],
                                      for_digest=args.prepare_digest)
            for item in candidates:
                if len(BoundedSession.events) >= REQUEST_LIMIT or time.monotonic() + 12 >= BoundedSession.deadline:
                    result["details_deferred"] = "request_or_time_budget"
                    break
                time.sleep(max(2, min(10, float(os.environ.get('AVITO_CARD_DELAY_SECONDS', '4')))))
                url = "https://www.avito.ru" + urlsplit(item["urlPath"]).path
                response, protection = fetch_card(sessions[-1], url)
                card = {"id": item["id"], "status": response.status_code if response is not None else 0,
                        "protection": protection}
                result["cards"].append(card)
                card["checked_at"] = result["started"]
                card["listing"] = item
                if protection['blocked']:
                    card.update(availability='unknown', status_evidence='card_protection_not_cleared')
                    result['details_blocked'] = True
                    break
                card.update(page_facts(response.text, response.status_code))
                if response.status_code in (403, 429, 439) or card["availability"] == "blocked":
                    result["details_blocked"] = True
                    break
                if response.status_code != 200:
                    continue
                card.update(parse_card(response.text))
                card["reviewed_price"] = card.get("price")
                card["price_source"] = "card" if card.get("price") else "unknown"
    except Exception as exc:
        result["status"] = "failed"
        # Exception messages upstream may include challenge tokens or response bodies.
        result["error_type"] = type(exc).__name__
        import traceback
        result["trace"] = [{"file": Path(f.filename).name, "line": f.lineno,
                            "function": f.name} for f in traceback.extract_tb(exc.__traceback__)]
    finally:
        signal.alarm(0)
        for session in sessions:
            session.close()
        result["finished"] = dt.datetime.now(dt.timezone.utc).isoformat()
        (output / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps({"status": result["status"], "listings": len(result["listings"]),
                          "summary": str(output / "summary.json")}, ensure_ascii=False))
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
