"""A malformed verdict must say why, on the ledger and on the PR (#1629).

`_verdict_from_task` collapses five distinct handler failures into one `None`.
The collect loop recorded `status: malformed` with no reason and posted
nothing, so the intake PR sat with no verdict and no trace — five verdicts
were lost this way with nothing left to diagnose them from.
"""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "verdict_malformed_test", Path(__file__).with_name("ccc-skill-promotion.py"))
promotion = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = promotion
SPEC.loader.exec_module(promotion)

HEAD = "a" * 40
OTHER = "b" * 40


def task(output, status="succeeded"):
    return {"status": status, "result": {"output": output,
            "provenance": {"schemaVersion": "a2a.result.provenance.v1",
                           "workerKeyId": "fixture"}}}


class MalformedReasonTests(unittest.TestCase):
    """Each rejection path names itself, and none of them is 'unknown'."""

    def reason(self, t):
        return promotion._malformed_verdict_reason(t, HEAD)

    def test_missing_result_output(self):
        self.assertEqual(self.reason({"status": "succeeded", "result": {}}),
                         "no_result_output")

    def test_verdict_value_invalid(self):
        self.assertEqual(
            self.reason(task({"verdict": "maybe", "findings": [], "head_sha": HEAD})),
            "verdict_value_invalid")

    def test_findings_not_a_list(self):
        self.assertEqual(
            self.reason(task({"verdict": "approve", "findings": "none", "head_sha": HEAD})),
            "findings_not_a_list")

    def test_head_sha_missing(self):
        self.assertEqual(
            self.reason(task({"verdict": "approve", "findings": []})),
            "head_sha_missing")

    def test_head_sha_mismatch_is_distinct_from_missing(self):
        # A reviewer that read a superseded tree is a different failure from a
        # handler that never emitted the field; conflating them hides rebases.
        self.assertEqual(
            self.reason(task({"verdict": "approve", "findings": [], "head_sha": OTHER})),
            "head_sha_mismatch")

    def test_every_rejected_shape_agrees_with_the_gate(self):
        shapes = [
            {"status": "succeeded", "result": {}},
            task({"verdict": "maybe", "findings": [], "head_sha": HEAD}),
            task({"verdict": "approve", "findings": "none", "head_sha": HEAD}),
            task({"verdict": "approve", "findings": []}),
            task({"verdict": "approve", "findings": [], "head_sha": OTHER}),
        ]
        for t in shapes:
            with self.subTest(t=t):
                # The gate still rejects it ...
                self.assertIsNone(promotion._verdict_from_task(t, HEAD))
                # ... and the reason is specific, never the fallback.
                self.assertNotEqual(self.reason(t), "unknown")

    def test_reason_never_leaks_result_content(self):
        # The reason string is written to a public PR comment.
        secret = "ghp_" + "x" * 36
        t = task({"verdict": secret, "findings": [], "head_sha": HEAD, "note": secret})
        self.assertNotIn(secret, self.reason(t))

    def test_wellformed_verdict_is_untouched(self):
        good = task({"verdict": "approve", "findings": [], "head_sha": HEAD,
                     "reviewer_node": "r", "review_agent": "a", "review_model": "m"})
        self.assertIsNotNone(promotion._verdict_from_task(good, HEAD))


class MalformedVisibilityTests(unittest.TestCase):
    """The collect loop must record the reason and leave a trace on the PR."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.config = SimpleNamespace(
            promotion_state_dir=self.state, collect_window=4, repo="example/repo")
        self.ledger = self.state / "ledger.jsonl"
        self.ledger.write_text(json.dumps({
            "kind": "a2a-dispatch", "dispatched_task": "bad-task", "head_sha": HEAD,
            "reviewer_node": "reviewer", "node": "author",
            "pr_url": "https://github.com/example/repo/pull/141"}) + "\n")
        self.ledger.chmod(0o600)
        # head_sha absent -> malformed
        self.enterContext(patch.object(
            promotion, "_broker_task_gated",
            return_value=(task({"verdict": "approve", "findings": []}), "claimed")))
        self.posted = []
        self.enterContext(patch.object(
            promotion, "_pr_comment", side_effect=lambda c, pr, body: self.posted.append((pr, body))))
        self.enterContext(patch.object(promotion, "_pr_state", return_value="OPEN", create=True))

    def collect(self):
        return promotion._process_verdicts(self.config, dry_run=False)

    def records(self):
        return [json.loads(line) for line in self.ledger.read_text().splitlines()]

    def test_ledger_records_the_reason(self):
        self.collect()
        mal = [r for r in self.records()
               if r.get("kind") == "a2a-verdict" and r.get("status") == "malformed"]
        self.assertEqual(len(mal), 1)
        self.assertEqual(mal[0].get("reason"), "head_sha_missing")

    def test_outcome_carries_the_reason(self):
        out = [r for r in self.collect() if r.get("outcome") == "verdict-malformed"]
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].get("reason"), "head_sha_missing")

    def test_pr_gets_exactly_one_comment_across_repeated_runs(self):
        self.collect()
        self.collect()
        self.collect()
        bodies = [b for _, b in self.posted if "verdict gate" in b]
        self.assertEqual(len(bodies), 1, self.posted)
        self.assertIn("head_sha_missing", bodies[0])
        self.assertIn("bad-task", bodies[0])

    def test_verdict_is_still_withheld(self):
        self.collect()
        real = [r for r in self.records()
                if r.get("kind") == "a2a-verdict" and r.get("verdict")]
        self.assertEqual(real, [], "a malformed result must never become a verdict")


if __name__ == "__main__":
    unittest.main()
