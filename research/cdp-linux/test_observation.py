"""Offline checks: no external destinations contacted."""
import contextlib
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import diagnostic_proxy as proxy
from read_manual import read_page
from playwright.sync_api import sync_playwright


class TransportTests(unittest.TestCase):
    def test_captcha_allowlist_boundaries(self):
        for host in ('hcaptcha.com', 'js.hcaptcha.com', 'gcaptcha4.geetest.com',
                     'gcaptcha4.geevisit.com', 'gcaptcha4.gsensebot.com', 'static.geetest.com'):
            self.assertTrue(proxy.allowed_destination(host, '443'))
            self.assertFalse(proxy.allowed_destination(host, '80'))
        for host in ('evilgeetest.com', 'geetest.com.evil.com', 'google.com'):
            self.assertFalse(proxy.allowed_destination(host, '443'))

    def exercise(self, request):
        handler = object.__new__(proxy.Handler)
        handler.connection = Mock()
        handler.rfile = io.BytesIO(request)
        handler.server = SimpleNamespace(mark=0x2024, interface='eno1')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            handler.handle()
        return handler.connection, output.getvalue()

    def test_denied_has_no_dns_or_secret(self):
        with patch.object(proxy.socket, 'getaddrinfo') as dns:
            conn, log = self.exercise(b'CONNECT private.example.com:443 HTTP/1.1\r\nCookie: SECRET\r\n\r\n')
        dns.assert_not_called()
        self.assertIn(b'403', conn.sendall.call_args.args[0])
        self.assertNotIn('SECRET', log)
        self.assertNotIn('private.example', log)
        self.assertEqual(json.loads(log)['domain'], 'example.com')

    def test_allowed_but_nonpublic_is_rejected(self):
        with patch.object(proxy.socket, 'getaddrinfo', return_value=[(0,0,0,'',('127.0.0.1',443))]), \
             patch.object(proxy.socket, 'socket') as sock:
            conn, log = self.exercise(b'CONNECT www.avito.ru:443 HTTP/1.1\r\n\r\n')
        sock.assert_not_called()
        self.assertEqual(json.loads(log)['stage'], 'dns')
        self.assertIn(b'502', conn.sendall.call_args.args[0])

    def test_tunnel_error_never_injects_http(self):
        upstream = Mock()
        upstream.recv.side_effect = ConnectionResetError()
        with patch.object(proxy.socket, 'getaddrinfo', return_value=[(0,0,0,'',('8.8.8.8',443))]), \
             patch.object(proxy.socket, 'socket', return_value=upstream), \
             patch.object(proxy.select, 'select', return_value=([upstream],[],[])):
            conn, log = self.exercise(b'CONNECT www.avito.ru:443 HTTP/1.1\r\n\r\n')
        self.assertEqual(conn.sendall.call_count, 1)
        self.assertIn(b'200', conn.sendall.call_args.args[0])
        self.assertEqual(json.loads(log.splitlines()[-1])['stage'], 'tunnel')
        upstream.close.assert_called_once()

    def test_empty_probe(self):
        conn, log = self.exercise(b'\n')
        conn.sendall.assert_not_called()
        self.assertEqual(json.loads(log)['event'], 'empty_probe')


class ReaderTests(unittest.TestCase):
    def test_read_does_not_navigate_or_change_dom(self):
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            context = browser.new_context(offline=True)
            page = context.new_page()
            page.set_content('''<div data-marker="catalog-serp"><div data-marker="item" data-item-id="123">
                <a data-marker="item-title" href="https://www.avito.ru/kazan/test_123">Fixture</a>
                <meta itemprop="price" content="100"></div></div>''')
            requests = []
            page.on('request', lambda r: requests.append(r.url))
            before = page.content()
            result = read_page(page)
            self.assertEqual(result['state']['dom_count'], 1)
            self.assertEqual(result['items'][0]['id'], '123')
            self.assertEqual(page.content(), before)
            self.assertEqual(page.url, 'about:blank')
            self.assertEqual(requests, [])
            page.set_content('Доступ ограничен: проблема с IP')
            self.assertEqual(read_page(page)['items'], [])
            browser.close()


if __name__ == '__main__':
    unittest.main()
