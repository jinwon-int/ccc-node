"""Receipt delivery must recover across collect cycles without replaying verdicts."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "receipt_retry_test", Path(__file__).with_name("ccc-skill-promotion.py"))
promotion = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = promotion
SPEC.loader.exec_module(promotion)
REAL_REMOTE_READER = getattr(promotion, "_receipt_already_posted", None)


class ReceiptRetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.config = SimpleNamespace(promotion_state_dir=self.state, collect_window=4)
        self.head = "a" * 40
        self.row = {"kind": "a2a-dispatch", "dispatched_task": "fixture-task",
                    "head_sha": self.head, "reviewer_node": "reviewer", "node": "author",
                    "pr_url": "https://github.com/example/repo/pull/1"}
        self.task = {"status": "succeeded", "result": {
            "output": {"verdict": "approve", "head_sha": self.head, "head_sha2": self.head,
                       "findings": [], "reviewer_node": "reviewer", "review_agent": "fixture",
                       "review_model": "fixture"},
            "provenance": {"schemaVersion": "a2a.result.provenance.v1", "workerKeyId": "fixture"},
        }}
        self.ledger = self.state / "ledger.jsonl"
        self.ledger.write_text(json.dumps(self.row) + "\n")
        self.ledger.chmod(0o600)
        self.broker = self.enterContext(patch.object(
            promotion, "_broker_task_gated", return_value=(self.task, "claimed")))
        self.remote = self.enterContext(patch.object(
            promotion, "_receipt_already_posted", return_value=False, create=True))
        self.posted = []
        self.enterContext(patch.object(promotion, "_pr_comment", side_effect=self.post))
        self.fail_receipt = False

    def post(self, config, pr, body):
        if self.fail_receipt and "Signed A2A intake review receipt" in body:
            raise promotion.PromotionError("fixture-transient-error")
        self.posted.append(body)

    def collect(self, dry_run=False):
        return promotion._process_verdicts(self.config, dry_run=dry_run)

    def records(self):
        return [json.loads(line) for line in self.ledger.read_text().splitlines()]

    def test_transient_failure_retries_once_and_reports_truthfully(self):
        self.fail_receipt = True
        self.collect()
        self.assertFalse(any("signed receipt projected on this PR" in b for b in self.posted))
        self.fail_receipt = False
        self.collect()
        self.collect()
        self.assertEqual(sum("Signed A2A intake review receipt" in b for b in self.posted), 1)
        self.assertEqual(sum(r.get("kind") == "a2a-verdict" for r in self.records()), 1)

    def test_legacy_consumed_verdict_can_publish_missing_receipt(self):
        promotion._append_ledger(self.config, {"kind": "a2a-verdict", "task_id": "fixture-task",
                                             "status": "consumed", "verdict": "approve"})
        self.collect()
        self.assertEqual(sum("Signed A2A intake review receipt" in b for b in self.posted), 1)
        self.assertEqual(sum(r.get("kind") == "a2a-verdict" for r in self.records()), 1)

    def test_accepted_comment_with_lost_response_is_not_duplicated(self):
        self.fail_receipt = True
        self.collect()
        self.fail_receipt = False
        self.remote.return_value = True
        self.collect()
        self.collect()
        self.remote.assert_called_once()
        self.assertFalse(any("Signed A2A intake review receipt" in b for b in self.posted))
        self.assertEqual(sum(str(r.get("marker", "")).startswith("receipt:")
                             for r in self.records()), 1)

    def test_retry_dry_run_does_not_read_broker_or_write(self):
        self.fail_receipt = True
        self.collect()
        before = self.ledger.read_bytes()
        self.broker.reset_mock()
        self.collect(dry_run=True)
        self.broker.assert_not_called()
        self.remote.assert_not_called()
        self.assertEqual(self.ledger.read_bytes(), before)

    def test_retry_does_not_repeat_revision_dispatch(self):
        self.task["result"]["output"]["verdict"] = "revise"
        self.fail_receipt = True
        with patch.object(promotion, "_dispatch_intake_revise",
                          return_value={"outcome": "dispatched"}) as revise:
            self.collect()
            self.fail_receipt = False
            self.collect()
            revise.assert_called_once()
        self.assertEqual(sum("Signed A2A intake review receipt" in b for b in self.posted), 1)

    def test_failed_remote_read_stays_pending_without_posting(self):
        self.fail_receipt = True
        self.collect()
        self.fail_receipt = False
        self.remote.side_effect = promotion.PromotionError("fixture-read-failure")
        self.collect()
        self.assertFalse(any("Signed A2A intake review receipt" in b for b in self.posted))
        self.remote.side_effect = None
        self.collect()
        self.assertEqual(sum("Signed A2A intake review receipt" in b for b in self.posted), 1)

    def test_broker_mismatch_does_not_publish(self):
        self.fail_receipt = True
        self.collect()
        self.fail_receipt = False
        self.broker.return_value = (self.task, "mismatch")
        self.collect()
        self.remote.assert_not_called()
        self.assertFalse(any("Signed A2A intake review receipt" in b for b in self.posted))

    def test_missing_provenance_is_not_polled_forever(self):
        del self.task["result"]["provenance"]
        self.collect()
        self.broker.reset_mock()
        self.collect()
        self.broker.assert_not_called()

    def test_retry_window_rotates_and_new_verdicts_keep_progressing(self):
        self.config.collect_window = 1
        for task_id in ("old-1", "old-2"):
            promotion._append_ledger(self.config, {**self.row, "dispatched_task": task_id})
            promotion._append_ledger(self.config, {"kind": "a2a-verdict", "task_id": task_id,
                                                 "status": "consumed", "verdict": "approve"})
        self.fail_receipt = True
        self.collect()
        ids = [call.args[2] for call in self.broker.call_args_list]
        self.assertEqual(ids, ["old-1", "fixture-task"])
        self.broker.reset_mock()
        self.collect()
        self.assertEqual([call.args[2] for call in self.broker.call_args_list], ["old-2"])

    def test_remote_receipt_lookup_matches_full_body_across_pages(self):
        body = promotion._receipt_comment_markdown(
            promotion._build_intake_receipt(self.task, "fixture-task", self.head, "reviewer", "author"),
            "reviewer", "author")
        # Call the real reader, bypassing the fixture's network boundary stub.
        with patch.object(promotion, "_run", return_value=SimpleNamespace(stdout=json.dumps([
            [{"body": "fixture-task"}], [{"body": body}],
        ]).encode())):
            self.assertTrue(REAL_REMOTE_READER(SimpleNamespace(repo="example/repo"), "1", body))
        with patch.object(promotion, "_run", return_value=SimpleNamespace(stdout=b'[[{"body":"fixture-task"}]]')):
            self.assertFalse(REAL_REMOTE_READER(SimpleNamespace(repo="example/repo"), "1", body))


if __name__ == "__main__":
    unittest.main()
