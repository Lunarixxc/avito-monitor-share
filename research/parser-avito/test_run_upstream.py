"""Offline checks for spending guard and configuration; no Avito requests."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import run_upstream as runner

sys.path.insert(0, str(runner.ROOT / 'upstream'))


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.old_cwd = Path.cwd()
        self.addCleanup(os.chdir, self.old_cwd)
        self.root_patch = patch.object(runner, 'ROOT', Path(self.temporary.name))
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.env_patch = patch.dict(os.environ, {
            'SPFA_API_KEY': 'synthetic-test-key',
            'AVITO_PROXY': 'test:test@example.invalid:8080',
            'AVITO_SEARCH_URL': 'https://www.avito.ru/kazan/noutbuki',
        })
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def test_offline_check_does_not_connect(self):
        with patch.object(sys, 'argv', ['run_upstream.py']), \
                patch('socket.socket.connect', side_effect=AssertionError('unexpected network')), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(runner.main(), 0)
        self.assertEqual(json.loads(out.getvalue())['network_requests'], 0)

    def test_invalid_search_port_returns_issue(self):
        with patch.dict(os.environ, {'AVITO_SEARCH_URL': 'https://www.avito.ru:invalid/'}):
            self.assertTrue(runner.read_config()[3])

    def test_purchase_is_denied_by_default(self):
        from parser.cookies.external_api import ExternalApiCookiesProvider
        with patch.object(sys, 'argv', ['run_upstream.py', '--once']), \
                patch.object(ExternalApiCookiesProvider, '_get_new_cookies') as purchase, \
                patch('socket.socket.connect', side_effect=AssertionError('unexpected network')), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(runner.main(), 2)
        purchase.assert_not_called()

    def test_authorized_purchase_failure_is_not_retried(self):
        from parser.cookies.external_api import ExternalApiCookiesProvider
        with patch.object(sys, 'argv', ['run_upstream.py', '--once', '--allow-cookie-purchase']), \
                patch.object(ExternalApiCookiesProvider, '_get_new_cookies', side_effect=RuntimeError('synthetic failure')) as purchase, \
                patch('socket.socket.connect', side_effect=AssertionError('unexpected network')), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(runner.main(), 2)
        self.assertEqual(purchase.call_count, 1)


if __name__ == '__main__':
    unittest.main()
