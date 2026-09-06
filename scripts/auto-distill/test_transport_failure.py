#!/usr/bin/env python3
"""Transport-failure classification regressions (#1561).

A Claude Code `--output-format json` envelope can carry `is_error: true` with
exit code 0 — the model was never reached (session limit, API error). Before
#1561 that envelope's human-readable sentence was handed downstream as if it
were the model's reply, so it surfaced as `no_json`: indistinguishable from a
model that genuinely emitted malformed JSON, and counted as one successful
request in usage accounting.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest


HERE = Path(__file__).resolve().parent
SOURCE = HERE / "auto-distill.py"

LIMIT_ENVELOPE = {
    "type": "result",
    "subtype": "success",
    "is_error": True,
    "total_cost_usd": 0,
    "modelUsage": {},
    "usage": {},
    "result": "You've hit your session limit · resets 7:10pm (Asia/Seoul)",
}

GOOD_ENVELOPE = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "total_cost_usd": 0.0372683,
    "modelUsage": {"claude-haiku-4-5-20251001": {"inputTokens": 10}},
    "usage": {"input_tokens": 10, "output_tokens": 4},
    "result": '{"items": []}',
}


def _load():
    spec = importlib.util.spec_from_file_location("transport_auto_distill", SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


AD = _load()


def _fake_cmd(tmpdir, envelope):
    """A stand-in model command that emits `envelope` and exits 0."""
    path = os.path.join(tmpdir, "fake-model")
    with open(path, "w") as fh:
        fh.write("#!/bin/sh\ncat >/dev/null\ncat <<'ENVELOPE'\n%s\nENVELOPE\n"
                 % json.dumps(envelope))
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
    return [path]


class TransportFailureClassificationTest(unittest.TestCase):
    def test_error_envelope_is_not_reported_as_malformed_model_output(self):
        """An unreached model must not look like a model that replied badly."""
        with tempfile.TemporaryDirectory() as tmp:
            data, (err, _usage, _raw) = AD.extract_json(
                "prompt", _fake_cmd(tmp, LIMIT_ENVELOPE), 30)
        self.assertIsNone(data)
        self.assertIsNotNone(err)
        self.assertNotEqual(
            err, "no_json",
            "transport failure was misclassified as malformed model output")
        self.assertTrue(
            err.startswith("model_unavailable"),
            "expected a dedicated transport-failure reason, got %r" % err)

    def test_error_envelope_body_is_not_leaked_into_the_reason(self):
        """The reason stays body-free — no provider prose, no reset times."""
        with tempfile.TemporaryDirectory() as tmp:
            _data, (err, _usage, _raw) = AD.extract_json(
                "prompt", _fake_cmd(tmp, LIMIT_ENVELOPE), 30)
        self.assertNotIn("session limit", err)
        self.assertNotIn("7:10pm", err)

    def test_unreached_model_is_not_counted_as_a_request(self):
        """Accounting must not report a healthy run when nothing was called."""
        with tempfile.TemporaryDirectory() as tmp:
            _data, (_err, usage, _raw) = AD.extract_json(
                "prompt", _fake_cmd(tmp, LIMIT_ENVELOPE), 30)
        self.assertEqual(
            (usage or {}).get("requests", 0), 0,
            "an envelope that never reached a model was counted as a request")

    def test_envelope_without_model_usage_is_a_transport_failure(self):
        """cost 0 + empty modelUsage means the call never reached a model."""
        envelope = dict(LIMIT_ENVELOPE)
        envelope["is_error"] = False
        envelope["result"] = '{"items": []}'
        with tempfile.TemporaryDirectory() as tmp:
            data, (err, usage, _raw) = AD.extract_json(
                "prompt", _fake_cmd(tmp, envelope), 30)
        self.assertIsNone(data)
        self.assertTrue(
            (err or "").startswith("model_unavailable"),
            "empty modelUsage at zero cost was accepted as a real answer: %r" % err)
        self.assertEqual((usage or {}).get("requests", 0), 0)

    def test_successful_envelope_still_parses_and_is_accounted(self):
        """The guard must not swallow real answers (GREEN control)."""
        with tempfile.TemporaryDirectory() as tmp:
            data, (err, usage, _raw) = AD.extract_json(
                "prompt", _fake_cmd(tmp, GOOD_ENVELOPE), 30)
        self.assertIsNone(err)
        self.assertEqual(data, {"items": []})
        self.assertEqual(usage["requests"], 1)
        self.assertEqual(usage["models"], ["claude-haiku-4-5-20251001"])
        self.assertGreater(usage["costUsd"], 0)


if __name__ == "__main__":
    unittest.main()
