"""The verdict comment must not be less complete than the verdict (#1770).

`_verdict_comment_body` rendered `findings[:8]` and told the reader the rest
were "in the broker record". A verdict keeps up to sixteen findings, the ledger
verdict row stored only a count, and broker task results do not outlive the
review — so the overflow of any review with more than eight findings became
unrecoverable, and the comment went on pointing at an empty record. Five open
intake PRs lost thirteen findings that way.

These tests pin the comment limit to the verdict limit, and pin the recording
that keeps findings machine-readable when no revision round consumes them.
"""
import importlib.util
import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "verdict_findings_limit_test", Path(__file__).with_name("ccc-skill-promotion.py"))
promotion = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = promotion
SPEC.loader.exec_module(promotion)

HEAD = "f" * 40


def findings(count: int) -> list[dict[str, str]]:
    return [
        {"severity": "major", "area": "quality", "note": f"finding number {n}"}
        for n in range(1, count + 1)
    ]


def body(count: int) -> str:
    return promotion._verdict_comment_body(
        "revise", findings(count), "nosuk", "task-1", "")


class CommentLimitTests(unittest.TestCase):
    def test_the_two_limits_are_one_constant(self):
        """A comment cap below the verdict cap is the whole defect."""
        self.assertEqual(promotion._MAX_VERDICT_FINDINGS, 16)

    def test_every_finding_a_verdict_keeps_is_rendered(self):
        text = body(promotion._MAX_VERDICT_FINDINGS)
        for n in range(1, promotion._MAX_VERDICT_FINDINGS + 1):
            self.assertIn(f"finding number {n}", text)

    def test_nine_findings_no_longer_lose_the_ninth(self):
        """The exact loss shape seen on PRs #80, #81 and #196."""
        text = body(9)
        self.assertIn("finding number 9", text)
        self.assertNotIn("more findings", text)

    def test_thirteen_findings_no_longer_lose_five(self):
        """The loss shape seen on PRs #90 and #106."""
        text = body(13)
        for n in range(9, 14):
            self.assertIn(f"finding number {n}", text)

    def test_no_comment_ever_points_at_the_broker_record(self):
        for count in [1, 8, 9, 16, 30]:
            with self.subTest(count=count):
                self.assertNotIn("broker record", body(count))

    def test_overflow_note_names_a_durable_source(self):
        text = body(promotion._MAX_VERDICT_FINDINGS + 4)
        self.assertIn("4 further findings", text)
        self.assertIn("publisher ledger", text)

    def test_verdict_parsing_and_rendering_agree_on_the_limit(self):
        """End to end: whatever _verdict_from_task keeps, the comment shows."""
        output = {
            "verdict": "revise",
            "head_sha": HEAD,
            "findings": findings(40),
            "schema": "skills.skill-intake-review.v1",
            "skillName": "s1",
            "sourceTreeSha256": "0" * 64,
            "taskId": "task-1",
        }
        task = {"status": "succeeded", "result": {
            "output": output,
            "provenance": {"schemaVersion": "a2a.result.provenance.v1",
                           "workerKeyId": "fixture"}}}
        parsed = promotion._verdict_from_task(task, HEAD)
        self.assertIsNotNone(parsed)
        _, kept = parsed
        text = promotion._verdict_comment_body("revise", kept, "nosuk", "task-1", "")
        self.assertEqual(len(kept), promotion._MAX_VERDICT_FINDINGS)
        for finding in kept:
            self.assertIn(finding["note"], text)


class SkipPreservationTests(unittest.TestCase):
    """Findings must survive every skip, because a skipped round is exactly
    when nothing else will ever read them back."""

    def _record(self, code: str) -> list[dict]:
        written: list[dict] = []
        cfg = types.SimpleNamespace(revise_substitute_after_days=0)
        with patch.multiple(
            promotion,
            _revise_dispatch_target=lambda r: (
                "gwakga", "s1", "abc123abc123", "42", "claude", HEAD, "https://x/42"),
            _append_ledger=lambda c, record: written.append(record),
        ):
            promotion._record_deferred_revise(
                cfg, [], {"dispatched_task": "t"}, findings(3), "nosuk",
                {"outcome": "revise-skipped", "code": code})
        return written

    def test_findings_survive_every_skip_code(self):
        for code in promotion._REVISE_SKIP_NOTES:
            with self.subTest(code=code):
                written = self._record(code)
                self.assertEqual(len(written), 1, f"{code} lost its findings")
                self.assertEqual(len(written[0]["findings"]), 3)

    def test_only_retryable_codes_reach_the_sweep(self):
        now = datetime.now(timezone.utc)
        for code in promotion._REVISE_SKIP_NOTES:
            with self.subTest(code=code):
                row = self._record(code)[0]
                due = promotion._deferred_revise_due([row], now)
                expected = [row] if code in promotion._REVISE_DEFERRABLE_CODES else []
                self.assertEqual(due, expected)

    def test_recording_a_row_does_not_imply_retrying_it(self):
        """The guarantee the two behaviours are deliberately separate."""
        recorded = {c for c in promotion._REVISE_SKIP_NOTES if self._record(c)}
        self.assertTrue(recorded > promotion._REVISE_DEFERRABLE_CODES)


if __name__ == "__main__":
    unittest.main()
