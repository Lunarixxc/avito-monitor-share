"""Offline safeguards; never connects to CDP or Avito."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import probe


class ProbeTests(unittest.TestCase):
    def report(self):
        return dict(documents=[], restricted_responses={}, goto_calls=0, outcome='environment_error')

    def fixture(self):
        pw = MagicMock()
        browser = pw.__enter__.return_value.chromium.connect_over_cdp.return_value
        page = MagicMock()
        page.url = probe.SEARCH
        page.goto.return_value.status = 200
        context = MagicMock()
        context.pages = [page]
        browser.contexts = [context]
        browser.version = 'test'
        return pw, browser, context, page

    def test_one_navigation_existing_context_no_close(self):
        pw, browser, context, page = self.fixture()
        clean = dict(catalog_present=True, dom_count=5, block_markers=[])
        report = self.report()
        with patch.object(probe, 'endpoint_check', return_value={}), \
                patch.object(probe, 'sync_playwright', return_value=pw), \
                patch.object(probe, 'snapshot', return_value=clean), \
                patch.object(probe, 'extract_rows', return_value=[{'id': '1'}]):
            probe.run(report)
        self.assertEqual(report['outcome'], 'success')
        self.assertEqual(page.goto.call_count, 1)
        browser.new_context.assert_not_called()
        context.new_page.assert_not_called()
        context.close.assert_not_called()
        browser.close.assert_not_called()
        pw.__enter__.return_value.chromium.connect_over_cdp.assert_called_once_with(
            probe.ENDPOINT, timeout=10000, no_defaults=True, is_local=False)

    def test_attach_block_prevents_navigation(self):
        pw, _, _, page = self.fixture()
        report = self.report()
        with patch.object(probe, 'endpoint_check', return_value={}), \
                patch.object(probe, 'sync_playwright', return_value=pw), \
                patch.object(probe, 'snapshot', return_value={'block_markers': ['captcha']}):
            probe.run(report)
        self.assertEqual(report['outcome'], 'manual_ok_attach_blocked')
        page.goto.assert_not_called()

    def test_http_block_no_wait_or_extraction_or_retry(self):
        pw, _, _, page = self.fixture()
        page.goto.return_value.status = 403
        report = self.report()
        clean = dict(catalog_present=True, dom_count=5, block_markers=[])
        def goto(*args, **kwargs):
            response = page.goto.return_value
            response.frame = page.main_frame
            response.url = probe.SEARCH + '?token=SECRET'
            response.request.is_navigation_request.return_value = True
            page.on.call_args.args[1](response)
            return response
        page.goto.side_effect = goto
        with patch.object(probe, 'endpoint_check', return_value={}), \
                patch.object(probe, 'sync_playwright', return_value=pw), \
                patch.object(probe, 'snapshot', return_value=clean), \
                patch.object(probe, 'extract_rows') as extract:
            probe.run(report)
        self.assertEqual(report['outcome'], 'manual_ok_cdp_navigation_blocked')
        self.assertEqual(page.goto.call_count, 1)
        page.wait_for_function.assert_not_called()
        extract.assert_not_called()
        self.assertNotIn('SECRET', json.dumps(report))

    def test_manual_failed_and_persistent_claim_no_connection(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(probe, 'ROOT', Path(tmp)), \
                patch('sys.argv', ['probe.py', '--run-once', '--manual-result', 'blocked']), \
                patch.object(probe, 'run') as run, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(probe.main(), 2)
            result = json.loads((Path(tmp) / 'results/result.json').read_text())
            self.assertEqual(result['outcome'], 'manual_failed_no_cdp')
            self.assertEqual(probe.main(), 2)
            run.assert_not_called()

    def test_timeout_never_retries(self):
        pw, _, _, page = self.fixture()
        page.goto.side_effect = probe.PlaywrightTimeout('SECRET must not be logged')
        report = self.report()
        clean = dict(catalog_present=True, dom_count=5, block_markers=[])
        with patch.object(probe, 'endpoint_check', return_value={}), \
                patch.object(probe, 'sync_playwright', return_value=pw), \
                patch.object(probe, 'snapshot', return_value=clean), \
                patch.object(probe, 'extract_rows', return_value=[]):
            probe.run(report)
        self.assertEqual(report['outcome'], 'inconclusive_navigation_error')
        self.assertEqual(page.goto.call_count, 1)
        self.assertNotIn('SECRET', json.dumps(report))

    def test_redaction(self):
        self.assertEqual(probe.safe_url(probe.SEARCH + '?token=SECRET#SECRET'), probe.SEARCH)
        self.assertNotIn('SECRET', probe.safe_url('https://www.avito.ru/challenge/SECRET?token=SECRET'))
        self.assertNotIn('SECRET', probe.safe_url('https://SECRET:SECRET@elsewhere.test/SECRET'))


if __name__ == '__main__':
    unittest.main()
