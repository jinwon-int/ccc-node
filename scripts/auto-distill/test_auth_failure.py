#!/usr/bin/env python3
"""Auth-failure classification, cause tokens and stall alerting (#2714).

2026-10-28 one node's extractor CLI exited rc=3 with
``OAuth refresh failed … invalid_grant`` for twelve days. The failure was
counted as a model-quality failure (``model_exit_3``), so every session was
dead-lettered after three ticks, the cause was cut away by
``extract_failure_reason``, and nothing told the owner.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock


HERE = Path(__file__).resolve().parent
SOURCE = HERE / "auto-distill.py"

INVALID_GRANT = ("OAuth refresh failed for xai: xAI OAuth token refresh failed "
                 "(HTTP 400): invalid_grant: Invalid or unknown refresh token")


def _load():
    spec = importlib.util.spec_from_file_location("auth_auto_distill", SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


AD = _load()


def _failing_cmd(tmpdir, stderr, rc=3, stdout=""):
    path = os.path.join(tmpdir, "fake-model")
    with open(path, "w") as fh:
        fh.write("#!/bin/sh\ncat >/dev/null\nprintf '%%s' '%s'\nprintf '%%s\\n' '%s' >&2\nexit %d\n"
                 % (stdout.replace("'", ""), stderr.replace("'", ""), rc))
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
    return [path]


class ClassifyTest(unittest.TestCase):
    def test_fixed_tokens(self):
        cases = {
            INVALID_GRANT: "auth_invalid_grant",
            "OAuth token refresh failed: upstream said no": "auth_oauth_refresh",
            "Error: HTTP 401 Unauthorized": "auth_401",
            "request failed with status code 403": "auth_403",
            "Invalid API key · Please run /login": "auth_invalid_key",
            "authentication failed for provider": "auth_unauthorized",
            "You've hit your session limit": "limit",
            "Error: 429 Too Many Requests": "limit",
            "connect ECONNREFUSED 127.0.0.1:443": "network",
        }
        for text, token in cases.items():
            with self.subTest(text=text):
                self.assertEqual(AD.classify_failure_text(text), token)

    def test_quality_text_is_not_classified(self):
        for text in ("Traceback: KeyError 'items'", "model returned garbage",
                     "port 4013 busy", ""):
            with self.subTest(text=text):
                self.assertIsNone(AD.classify_failure_text(text))


class ExtractJsonTest(unittest.TestCase):
    def test_invalid_grant_exit_is_transport_with_cause(self):
        with tempfile.TemporaryDirectory() as tmp:
            data, (err, usage, _raw) = AD.extract_json(
                "prompt", _failing_cmd(tmp, INVALID_GRANT), 30)
        self.assertIsNone(data)
        self.assertEqual(err, "model_unavailable:auth_invalid_grant")
        self.assertEqual(usage, {})
        self.assertTrue(AD.is_transport_failure(err))
        self.assertEqual(AD.extract_failure_reason(err), "model_unavailable")
        self.assertEqual(AD.extract_failure_cause(err), "auth_invalid_grant")

    def test_auth_message_on_stdout_is_classified(self):
        with tempfile.TemporaryDirectory() as tmp:
            _data, (err, _u, _r) = AD.extract_json(
                "prompt", _failing_cmd(tmp, "", rc=1,
                                       stdout="Invalid API key - Please run /login"), 30)
        self.assertEqual(err, "model_unavailable:auth_invalid_key")

    def test_unclassified_exit_stays_a_quality_failure_without_cause(self):
        with tempfile.TemporaryDirectory() as tmp:
            _data, (err, _u, _r) = AD.extract_json(
                "prompt", _failing_cmd(tmp, "secret body text here"), 30)
        self.assertTrue(err.startswith("model_exit_3:"))
        self.assertFalse(AD.is_transport_failure(err))
        self.assertEqual(AD.extract_failure_reason(err), "model_exit_3")
        # model/CLI text after the colon is never kept as a cause
        self.assertIsNone(AD.extract_failure_cause(err))

    def test_auth_error_envelope_carries_auth_cause(self):
        env = json.dumps({"type": "result", "is_error": True,
                          "result": "Invalid API key · Please run /login"})
        _u, _inner, token = AD.unwrap_claude_envelope(env)
        self.assertEqual(token, "auth_invalid_key")
        limit = json.dumps({"type": "result", "is_error": True,
                            "result": "You've hit your session limit"})
        self.assertEqual(AD.unwrap_claude_envelope(limit)[2], "error_envelope")


class BudgetTest(unittest.TestCase):
    def test_auth_failures_never_spend_the_quality_budget(self):
        entry = {}
        err = "model_unavailable:auth_invalid_grant"
        for _ in range(AD.EXTRACT_FAIL_BUDGET + 2):
            entry, failure = AD.record_extract_failure(
                entry, path="/x", snapshot_size=10, snapshot_mtime=1.0, error=err)
        self.assertEqual(failure["attempts"], 0)
        self.assertEqual(failure["transport_attempts"], AD.EXTRACT_FAIL_BUDGET + 2)
        self.assertFalse(failure["dead_lettered"])
        self.assertTrue(failure["transport"])
        self.assertEqual(failure["reason"], "model_unavailable")
        self.assertEqual(failure["cause"], "auth_invalid_grant")

    def test_quality_failure_has_null_cause(self):
        _entry, failure = AD.record_extract_failure(
            {}, path="/x", snapshot_size=10, snapshot_mtime=1.0,
            error="model_exit_3: some stderr body")
        self.assertIsNone(failure["cause"])
        self.assertEqual(failure["reason"], "model_exit_3")


class RunHealthTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, "health.json")
        self.spool = os.path.join(self.tmp.name, "spool")
        patcher = mock.patch.object(AD, "log_audit", lambda rec: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, ok=0, failed=0, causes=None, threshold=3):
        return AD.update_run_health(ok=ok, failed=failed, causes=causes or {},
                                    node="node-a", engine="piri", path=self.state,
                                    threshold=threshold, spool=self.spool)

    def _records(self):
        if not os.path.isdir(self.spool):
            return []
        out = []
        for name in sorted(os.listdir(self.spool)):
            with open(os.path.join(self.spool, name)) as fh:
                out.append(json.load(fh))
        return out

    def test_alerts_once_after_threshold_then_recovers_once(self):
        causes = {"auth_invalid_grant": 2}
        self.assertIsNone(self._run(failed=2, causes=causes))
        self.assertIsNone(self._run(failed=2, causes=causes))
        self.assertEqual(self._run(failed=2, causes=causes), "alert")
        self.assertIsNone(self._run(failed=2, causes=causes))  # no repeat
        records = self._records()
        self.assertEqual(len(records), 1)
        alert = records[0]
        self.assertEqual(alert["event"], "AutoDistill")
        self.assertEqual(alert["tier"], "notice")
        self.assertEqual(alert["node"], "node-a")
        self.assertIn("auth_invalid_grant×6", alert["text"])
        self.assertTrue(alert["dedup"].startswith("AutoDistill:stalled:"))
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(
            self.spool, os.listdir(self.spool)[0])).st_mode), 0o600)

        self.assertEqual(self._run(ok=1), "recovered")
        records = self._records()
        self.assertEqual(len(records), 2)
        recovered = [r for r in records if r["dedup"].startswith("AutoDistill:recovered:")]
        self.assertEqual(len(recovered), 1)
        with open(self.state) as fh:
            self.assertEqual(json.load(fh)["fail_runs"], 0)
        self.assertIsNone(self._run(ok=1))  # no second recovery notice

    def test_idle_runs_neither_extend_nor_reset_the_streak(self):
        self._run(failed=1)
        self._run(failed=1)
        self.assertIsNone(self._run())  # nothing to do this tick
        self.assertEqual(self._run(failed=1), "alert")

    def test_success_before_threshold_resets_silently(self):
        self._run(failed=1)
        self._run(failed=1)
        self.assertIsNone(self._run(ok=1))
        self.assertIsNone(self._run(failed=1))
        self.assertEqual(self._records(), [])

    def test_threshold_zero_disables_alerts(self):
        for _ in range(10):
            self.assertIsNone(self._run(failed=1, threshold=0))
        self.assertEqual(self._records(), [])

    def test_unwritable_spool_retries_next_run(self):
        blocker = os.path.join(self.tmp.name, "blocker")
        with open(blocker, "w") as fh:
            fh.write("x")
        self.spool = os.path.join(blocker, "spool")  # parent is a file
        for _ in range(2):
            self._run(failed=1)
        self.assertEqual(self._run(failed=1), "alert")
        with open(self.state) as fh:
            self.assertFalse(json.load(fh)["alerted"])
        self.spool = os.path.join(self.tmp.name, "spool")
        self.assertEqual(self._run(failed=1), "alert")
        self.assertEqual(len(self._records()), 1)

    def test_env_threshold(self):
        with mock.patch.dict(os.environ, {"CCC_AUTO_DISTILL_ALERT_RUNS": "2"}):
            self.assertEqual(AD._alert_threshold(), 2)
        with mock.patch.dict(os.environ, {"CCC_AUTO_DISTILL_ALERT_RUNS": "junk"}):
            self.assertEqual(AD._alert_threshold(), AD.DISTILL_ALERT_RUNS_DEFAULT)


if __name__ == "__main__":
    unittest.main()
