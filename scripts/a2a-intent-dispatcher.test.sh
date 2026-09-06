#!/usr/bin/env bash
# Synthetic payloads only; no broker, model, or live handler execution.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
python3 - "$HERE/a2a-intent-dispatcher.sh" <<'PY'
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest

DISPATCHER = sys.argv.pop()


class DispatcherTest(unittest.TestCase):
    def setUp(self):
        self.fixture = tempfile.TemporaryDirectory(prefix="a2a-dispatch-test-")
        self.addCleanup(self.fixture.cleanup)
        self.root = Path(self.fixture.name)
        self.payloads = self.root / "payloads"
        self.payloads.mkdir()
        self.handler = self.root / "handler.sh"
        self.env = dict(os.environ, TMPDIR=str(self.payloads),
                        INTAKE_REVIEW_HANDLER=str(self.handler),
                        INTAKE_REVISE_HANDLER=str(self.handler),
                        DEFAULT_TASK_HANDLER=f"bash {self.handler}")
        self.write_handler('cat; exit "${TEST_EXIT:-0}"')

    def write_handler(self, body):
        self.handler.write_text('#!/usr/bin/env bash\nset -euo pipefail\n'
                                # Handler must never see the payload pathname
                                # or the extra dispatcher descriptor, even while
                                # it is still running, not just after completion.
                                'test -z "$(ls -A "$TMPDIR")" || exit 91\n'
                                'test ! -e /proc/$$/fd/3 || exit 92\n' + body + '\n')
        self.handler.chmod(0o700)

    def run_dispatch(self, body):
        return subprocess.run(['bash', DISPATCHER], input=body, env=self.env,
                              capture_output=True, timeout=5)

    def test_routes_preserve_bytes_and_exit_status(self):
        for intent in ('skills-intake-review', 'skills_intake_review',
                       'skills-intake-revise', 'skills_intake_revise', 'analyze', None):
            for code in (0, 17):
                with self.subTest(intent=intent, code=code):
                    self.env['TEST_EXIT'] = str(code)
                    payload = json.dumps({'intent': intent, 'payload': '합성 입력'}).encode() + b'\n \n'
                    result = self.run_dispatch(payload)
                    self.assertEqual(result.returncode, code, result.stderr)
                    self.assertEqual(result.stdout, payload)
                    self.assertEqual(list(self.payloads.iterdir()), [])

    def test_invalid_input_is_cleaned(self):
        for body in (b'', b'{broken'):
            with self.subTest(body=body):
                result = self.run_dispatch(body)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, b'')
                self.assertEqual(list(self.payloads.iterdir()), [])

    def test_missing_handlers_are_cleaned(self):
        self.handler.unlink()
        for intent in ('skills-intake-review', 'skills-intake-revise', 'analyze'):
            with self.subTest(intent=intent):
                result = self.run_dispatch(json.dumps({'intent': intent}).encode())
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(list(self.payloads.iterdir()), [])

    def test_exec_preserves_pid_and_signal_status(self):
        self.write_handler('cat >/dev/null; printf "%s\\n" "$$"; kill -TERM "$$"')
        with subprocess.Popen(['bash', DISPATCHER], env=self.env, stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
            output, errors = proc.communicate(b'{"intent":"analyze"}', timeout=5)
            self.assertEqual(output.strip(), str(proc.pid).encode(), errors)
            self.assertEqual(proc.returncode, -signal.SIGTERM, errors)
        self.assertEqual(list(self.payloads.iterdir()), [])

    def test_cleanup_failure_does_not_dispatch(self):
        fakebin = self.root / 'bin'
        fakebin.mkdir()
        remove = fakebin / 'rm'
        remove.write_text('#!/bin/sh\nexit 1\n')
        remove.chmod(0o700)
        self.env['PATH'] = str(fakebin) + os.pathsep + self.env['PATH']
        result = self.run_dispatch(b'{"intent":"analyze"}')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b'')
        self.assertIn(b'payload cleanup failed', result.stderr)
        # The intentionally failed unlink leaves an owner-only disposable file
        # for fixture cleanup; never pretend its removal succeeded.
        for path in self.payloads.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


unittest.main()
PY
