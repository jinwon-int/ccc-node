"""A receipt that can never be delivered must leave the retry queue (#1618).

`pending` was recorded and re-queued every collect run with no terminal state,
no attempt cap and no PR-liveness check, so a task whose broker result never
arrives is retried forever. Field case: fleet-skills#104 was CLOSED and still
re-polled on every nightly run for a week.
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
    "receipt_terminal_test", Path(__file__).with_name("ccc-skill-promotion.py"))
promotion = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = promotion
SPEC.loader.exec_module(promotion)


class ReceiptTerminalStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.config = SimpleNamespace(
            promotion_state_dir=self.state, collect_window=4, repo="example/repo")
        self.head = "a" * 40
        self.ledger = self.state / "ledger.jsonl"
        rows = [
            {"kind": "a2a-dispatch", "dispatched_task": "stuck-task", "head_sha": self.head,
             "reviewer_node": "reviewer", "node": "author",
             "pr_url": "https://github.com/example/repo/pull/104"},
            {"kind": "a2a-verdict", "task_id": "stuck-task", "status": "consumed",
             "verdict": "approve"},
        ]
        self.ledger.write_text("".join(json.dumps(r) + "\n" for r in rows))
        self.ledger.chmod(0o600)
        # The broker never returns a usable result — the condition that made
        # the old code loop forever.
        self.enterContext(patch.object(
            promotion, "_broker_task_gated", return_value=(None, "missing")))
        self.pr_state = self.enterContext(patch.object(
            promotion, "_pr_state", return_value="OPEN"))

    def collect(self):
        return promotion._process_verdicts(self.config, dry_run=False)

    def outcomes(self):
        return [r.get("outcome") for r in self.collect()]

    def statuses(self):
        return [json.loads(line).get("status")
                for line in self.ledger.read_text().splitlines()
                if json.loads(line).get("kind") == "a2a-receipt"]

    # --- PR reap -----------------------------------------------------------

    def test_closed_pr_retires_the_receipt(self):
        self.pr_state.return_value = "CLOSED"
        self.assertIn("receipt-pr-closed", self.outcomes())
        self.assertNotIn("receipt-pending", self.outcomes())

    def test_merged_pr_retires_the_receipt(self):
        self.pr_state.return_value = "MERGED"
        self.assertIn("receipt-pr-closed", self.outcomes())

    def test_retired_receipt_is_not_requeued_on_later_runs(self):
        self.pr_state.return_value = "CLOSED"
        self.collect()
        self.pr_state.reset_mock()
        self.assertEqual(self.outcomes(), [])
        # Nothing is even looked up again — the task left the queue for good.
        self.pr_state.assert_not_called()

    def test_open_pr_still_retries(self):
        self.assertIn("receipt-pending", self.outcomes())
        self.assertIn("receipt-pending", self.outcomes())

    def test_unreadable_pr_state_does_not_retire(self):
        # A transient gh failure must never retire a receipt that is still owed.
        self.pr_state.return_value = None
        self.assertIn("receipt-pending", self.outcomes())

    def test_closed_pr_is_reaped_without_a_broker_round_trip(self):
        self.pr_state.return_value = "CLOSED"
        with patch.object(promotion, "_broker_task_gated") as broker:
            self.collect()
            broker.assert_not_called()

    # --- age expiry --------------------------------------------------------

    def test_receipt_expires_after_the_grace_window(self):
        self.collect()  # records the first attempt at "now"
        self.assertEqual(self.statuses(), ["pending"])
        with patch.object(promotion, "_receipt_is_stale", return_value=True):
            self.assertIn("receipt-expired", self.outcomes())
        self.pr_state.reset_mock()
        self.assertEqual(self.outcomes(), [])

    def test_stale_check_uses_wall_clock_not_attempt_count(self):
        stale = promotion._receipt_is_stale
        self.assertFalse(stale("2026-09-01T00:00:00Z", now="2026-09-10T00:00:00Z"))
        self.assertTrue(stale("2026-08-01T00:00:00Z", now="2026-09-10T00:00:00Z"))

    def test_stale_boundary_is_the_documented_window(self):
        stale = promotion._receipt_is_stale
        days = promotion._RECEIPT_STALE_DAYS
        self.assertEqual(days, 14)
        self.assertFalse(stale("2026-09-01T00:00:00Z", now="2026-09-14T23:59:59Z"))
        self.assertTrue(stale("2026-09-01T00:00:00Z", now="2026-09-15T00:00:00Z"))

    def test_absent_or_unparsable_stamp_is_never_stale(self):
        stale = promotion._receipt_is_stale
        self.assertFalse(stale(None, now="2026-09-10T00:00:00Z"))
        self.assertFalse(stale("", now="2026-09-10T00:00:00Z"))
        self.assertFalse(stale("not-a-timestamp", now="2026-09-10T00:00:00Z"))
        self.assertFalse(stale("2026-08-01T00:00:00Z", now="garbage"))

    def test_first_attempt_is_the_earliest_not_the_latest(self):
        self.collect()
        self.collect()
        first = promotion._receipt_first_attempt(
            [json.loads(line) for line in self.ledger.read_text().splitlines()], "stuck-task")
        stamps = [json.loads(line).get("ts")
                  for line in self.ledger.read_text().splitlines()
                  if json.loads(line).get("kind") == "a2a-receipt"]
        self.assertEqual(len(stamps), 2)
        self.assertEqual(first, min(stamps))

    # --- boundaries --------------------------------------------------------

    def test_expired_is_distinct_from_unavailable(self):
        self.assertIn("expired", promotion._RECEIPT_TERMINAL_STATUSES)
        self.assertIn("pr-closed", promotion._RECEIPT_TERMINAL_STATUSES)
        self.assertIn("unavailable", promotion._RECEIPT_TERMINAL_STATUSES)
        self.assertNotIn("pending", promotion._RECEIPT_TERMINAL_STATUSES)

    def test_dry_run_neither_reaps_nor_writes(self):
        self.pr_state.return_value = "CLOSED"
        before = self.ledger.read_bytes()
        self.pr_state.reset_mock()
        results = promotion._process_verdicts(self.config, dry_run=True)
        self.assertIn("would-retry-receipt", [r.get("outcome") for r in results])
        self.pr_state.assert_not_called()
        self.assertEqual(self.ledger.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
