"""Real PTY + Chromium regressions using offline HTML, never Avito."""
import errno
import json
import os
from pathlib import Path
import pty
import select
import shlex
import shutil
import signal
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from probe import classify


class ManualTests(unittest.TestCase):
    def run_session(self, action, wrapper=False, body='Доступ ограничен: проблема с IP'):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            fixture = state / 'fixture.html'
            fixture.write_text('<html><body><h1>' + body + '</h1></body></html>')
            args = ['--offline-html', str(fixture), '--state-dir', str(state), '--manual-timeout', '2' if action == 'timeout' else '15']
            if wrapper:
                for name in ('manual-review.sh', 'probe.py'):
                    shutil.copy(ROOT / name, state / name)
                (state / '.venv').symlink_to(ROOT / '.venv', target_is_directory=True)
                cmd = ['bash', str(state / 'manual-review.sh'), *args]
            else:
                cmd = ['timeout', '--foreground', '--signal=INT', '--kill-after=10s', '30',
                       sys.executable, str(ROOT / 'probe.py'), '--manual', *args]
            pid, fd = pty.fork()
            if pid == 0:
                # Keep a foreground shell above timeout, as in an SSH session.
                os.execvp('bash', ['bash', '-c', shlex.join(cmd) + '; exit $?'])
            sent = done = False
            output = b''
            try:
                deadline = time.monotonic() + 35
                while time.monotonic() < deadline:
                    if select.select([fd], [], [], .1)[0]:
                        try:
                            chunk = os.read(fd, 65536)
                        except OSError as exc:
                            if exc.errno == errno.EIO:
                                chunk = b''
                            else:
                                raise
                        output += chunk
                    if not sent and b'Press Enter IN THIS SSH TERMINAL' in output:
                        sent = True
                        if action == 'enter':
                            os.write(fd, b'\n')
                        elif action == 'interrupt':
                            os.write(fd, b'\x03')
                        elif action == 'eof':
                            os.write(fd, b'\x04')
                    got, status = os.waitpid(pid, os.WNOHANG)
                    if got:
                        done = True
                        break
                self.assertTrue(done, output.decode(errors='replace'))
                self.assertTrue(sent, output.decode(errors='replace'))
                records = list((state / 'results').glob('*/result.json'))
                self.assertEqual(len(records), 1)
                result = json.loads(records[0].read_text())
                expected = {'enter': 'enter', 'interrupt': 'SIGINT', 'eof': 'stdin_eof', 'timeout': 'manual_timeout'}[action]
                self.assertEqual(result['finish_reason'], expected)
                self.assertEqual(result['status'], 'IP_BLOCKED')
                self.assertTrue(result['offline'])
                self.assertEqual(json.loads((state/'private/blocked.json').read_text())['status'], 'IP_BLOCKED')
                for name in ('page.txt', 'page.html', 'page.png'):
                    self.assertTrue((records[0].parent / name).stat().st_size)
                # An unattended restart must stop before launching Chromium.
                import subprocess
                again = subprocess.run([sys.executable, str(ROOT/'probe.py'), '--state-dir', tmp], capture_output=True, timeout=5)
                self.assertNotEqual(again.returncode, 0)
                self.assertIn(b'Collection paused', again.stderr)
                if wrapper:
                    for port in (5901, 6080):
                        import socket
                        with socket.socket() as sock:
                            self.assertNotEqual(sock.connect_ex(('127.0.0.1', port)), 0)
            finally:
                if not done:
                    os.killpg(pid, signal.SIGTERM)
                    os.waitpid(pid, 0)
                os.close(fd)

    def test_enter_full_wrapper(self):
        self.run_session('enter', wrapper=True)

    def test_ctrl_c(self):
        self.run_session('interrupt')

    def test_eof(self):
        self.run_session('eof')

    def test_deadline(self):
        self.run_session('timeout')

    def test_classification(self):
        self.assertEqual(classify('Доступ ограничен: проблема с IP', 403), 'IP_BLOCKED')
        self.assertEqual(classify('Подтвердите, что вы не робот'), 'CAPTCHA')
        self.assertEqual(classify('', 429), 'access_restricted')
        self.assertIsNone(classify('Обычная страница', 200))


if __name__ == '__main__':
    unittest.main(verbosity=2)
