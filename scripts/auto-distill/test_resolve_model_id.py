#!/usr/bin/env python3
"""Model-call-free resolved-model-id helpers (#1521)."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))

from model_command import CLAUDE_ARGS, claude_model_alias, resolved_model_ids  # noqa: E402
from resolve_model_id import build_resolution  # noqa: E402

RESOLVER = HERE / "resolve_model_id.py"
VERIFIER = ROOT / "scripts/verify-auto-distill-receipt.py"
SOURCE = HERE / "auto-distill.py"
RECEIPT = HERE / "evaluation-receipt.json"
MODEL_ID = "claude-haiku-4-5-20251001"


def envelope(model_id: str = MODEL_ID, **extra) -> str:
    return json.dumps(
        {
            "type": "result",
            "result": "{}",
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "modelUsage": {model_id: {"inputTokens": 1}},
            **extra,
        }
    )


class ClaudeAliasTest(unittest.TestCase):
    def test_managed_claude_args_send_a_bare_alias(self) -> None:
        # The receipt's `model` is this alias; the resolution object must pin
        # what the provider resolved it to.
        self.assertEqual(claude_model_alias(CLAUDE_ARGS), "haiku")

    def test_equals_form_and_absence(self) -> None:
        self.assertEqual(claude_model_alias(("claude", "--model=opus")), "opus")
        self.assertIsNone(claude_model_alias(("claude", "-p", "--model")))
        self.assertIsNone(claude_model_alias(("piri", "-p")))


class ResolvedModelIdsTest(unittest.TestCase):
    def test_single_envelope(self) -> None:
        self.assertEqual(resolved_model_ids(envelope()), [MODEL_ID])

    def test_log_with_one_envelope_per_line_and_noise(self) -> None:
        text = "\n".join(
            [
                "eval start",
                envelope(),
                '{"type": "system", "modelUsage": {"ignored": {}}}',
                "{not json",
                envelope(),
            ]
        )
        self.assertEqual(resolved_model_ids(text), [MODEL_ID])

    def test_non_envelopes_yield_nothing(self) -> None:
        self.assertEqual(resolved_model_ids("plain text"), [])
        self.assertEqual(resolved_model_ids(json.dumps({"result": "x"})), [])
        self.assertEqual(resolved_model_ids(json.dumps({"type": "result", "modelUsage": 3})), [])


class BuildResolutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write(self, name: str, text: str) -> Path:
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_fragment_shape(self) -> None:
        log = self.write("eval.log", envelope())
        now = datetime(2026, 9, 5, 13, 1, 16, tzinfo=timezone.utc)
        self.assertEqual(
            build_resolution("haiku", [log], now=now),
            {
                "alias": "haiku",
                "resolved_id": MODEL_ID,
                "resolved_by": "claude-json-modelUsage:eval.log",
                "resolved_at": "2026-09-05T13:01:16Z",
            },
        )

    def test_no_id_and_ambiguous_ids_fail(self) -> None:
        empty = self.write("empty.log", "nothing here")
        with self.assertRaisesRegex(ValueError, "resolved_by=operator"):
            build_resolution("haiku", [empty])
        mixed = self.write("mixed.log", envelope() + "\n" + envelope("claude-opus-4-1"))
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            build_resolution("haiku", [mixed])
        alias = self.write("alias.log", envelope("haiku"))
        with self.assertRaisesRegex(ValueError, "bare alias"):
            build_resolution("haiku", [alias])

    def test_symlink_envelope_is_refused(self) -> None:
        real = self.write("real.log", envelope())
        link = self.root / "link.log"
        link.symlink_to(real)
        with self.assertRaisesRegex(ValueError, "unsafe"):
            build_resolution("haiku", [link])

    def test_cli_fragment_makes_the_canonical_receipt_verify_with_require_model(self) -> None:
        log = self.write("eval.log", envelope())
        run = subprocess.run(
            ["python3", str(RESOLVER), "--envelope", str(log)],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        fragment = json.loads(run.stdout)
        self.assertEqual(fragment["alias"], "haiku")
        self.assertEqual(fragment["resolved_id"], MODEL_ID)

        receipt = json.loads(RECEIPT.read_text(encoding="utf-8"))
        receipt["evaluation"]["model_resolution"] = fragment
        # The grafted fragment carries its own alias, so `evaluation.model` has
        # to move with it. The canonical receipt is free to hand the launcher a
        # fully-qualified id instead of an alias (TM-3322 does); inheriting that
        # value here would make the verifier reject the graft on a mismatch that
        # this test never meant to create.
        receipt["evaluation"]["model"] = fragment["alias"]
        # The fragment is stamped now; a re-issued receipt is issued afterwards.
        receipt["issued_at"] = "2099-01-01T00:00:00Z"
        target = self.root / "receipt.json"
        target.write_text(json.dumps(receipt), encoding="utf-8")
        verified = subprocess.run(
            [
                "python3",
                str(VERIFIER),
                "--source",
                str(SOURCE),
                "--receipt",
                str(target),
                "--require-model",
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(verified.returncode, 0, verified.stderr)
        self.assertIn(f"model={MODEL_ID}", verified.stdout)

    def test_cli_without_envelope_explains_manual_path(self) -> None:
        run = subprocess.run(
            ["python3", str(RESOLVER)],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(run.returncode, 2)
        self.assertIn("alias=haiku", run.stderr)
        self.assertIn("resolved_by=operator", run.stderr)


if __name__ == "__main__":
    unittest.main()
