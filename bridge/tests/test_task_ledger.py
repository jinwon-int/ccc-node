"""Unit tests for the persistent task ledger (Hermes-style task lifecycle)."""

import sys
import tempfile
import types
import unittest
from pathlib import Path

# Order-independent bootstrap: map the bridge dir as the `telegram_bot` package
# so this module imports cleanly regardless of which test file loads first.
BRIDGE_DIR = Path(__file__).resolve().parents[1]
if "telegram_bot" not in sys.modules:
    _pkg = types.ModuleType("telegram_bot")
    _pkg.__path__ = [str(BRIDGE_DIR)]
    sys.modules["telegram_bot"] = _pkg

from telegram_bot.core.task_ledger import (  # noqa: E402
    CANCELED,
    COMPLETED,
    INPUT_REQUIRED,
    INTERRUPTED,
    MAX_TERMINAL_OP_ATTEMPTS,
    TaskLedger,
    WORKING,
    default_task_ledger_path,
    ledger_path_for,
)


class TaskLedgerTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.ledger = TaskLedger(Path(self._td.name) / "sub" / "tasks.json")

    def _only_record(self):
        records = self.ledger.records()
        self.assertEqual(len(records), 1)
        return records[0]

    def test_create_registers_working_record(self):
        task_id = self.ledger.create(1, 2)
        rec = self._only_record()
        self.assertEqual(rec["task_id"], task_id)
        self.assertEqual(rec["state"], WORKING)
        self.assertIsNone(rec["status_message_id"])

    def test_finish_with_clean_cleanup_purges(self):
        task_id = self.ledger.create(1, 2)
        self.ledger.finish(task_id, COMPLETED, cleanup_done=True)
        self.assertEqual(self.ledger.records(), [])

    def test_finish_without_message_purges_even_if_cleanup_failed(self):
        task_id = self.ledger.create(1, 2)
        self.ledger.finish(task_id, CANCELED, cleanup_done=False)
        self.assertEqual(self.ledger.records(), [])  # nothing to clean up

    def test_finish_with_failed_cleanup_keeps_terminal_op(self):
        task_id = self.ledger.create(1, 2)
        self.ledger.set_status_message(task_id, 777)
        self.ledger.finish(task_id, COMPLETED, cleanup_done=False)
        rec = self._only_record()
        self.assertEqual(rec["state"], COMPLETED)
        self.assertEqual(rec["terminal_op"]["message_id"], 777)
        self.assertEqual(rec["terminal_op"]["chat_id"], 2)

    def test_finish_is_idempotent(self):
        task_id = self.ledger.create(1, 2)
        self.ledger.set_status_message(task_id, 777)
        self.ledger.finish(task_id, COMPLETED, cleanup_done=False)
        self.ledger.finish(task_id, CANCELED, cleanup_done=True)  # second: no-op
        rec = self._only_record()
        self.assertEqual(rec["state"], COMPLETED)
        self.ledger.finish("missing-task", COMPLETED)  # absent: no-op

    def test_nonterminal_state_transitions(self):
        task_id = self.ledger.create(1, 2)
        self.ledger.set_state(task_id, INPUT_REQUIRED)
        self.assertEqual(self._only_record()["state"], INPUT_REQUIRED)
        self.ledger.set_state(task_id, WORKING)
        self.assertEqual(self._only_record()["state"], WORKING)
        # set_state never applies terminal states
        self.ledger.set_state(task_id, COMPLETED)
        self.assertEqual(self._only_record()["state"], WORKING)

    def test_terminal_op_retry_bookkeeping(self):
        task_id = self.ledger.create(1, 2)
        self.ledger.set_status_message(task_id, 5)
        self.ledger.finish(task_id, COMPLETED, cleanup_done=False)
        ops = self.ledger.pending_terminal_ops()
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0][0], task_id)
        self.ledger.resolve_terminal_op(task_id, success=False)
        self.assertEqual(
            self.ledger.pending_terminal_ops()[0][1]["attempts"], 1
        )
        self.ledger.resolve_terminal_op(task_id, success=True)
        self.assertEqual(self.ledger.records(), [])

    def test_terminal_op_gives_up_past_attempt_cap(self):
        task_id = self.ledger.create(1, 2)
        self.ledger.set_status_message(task_id, 5)
        self.ledger.finish(task_id, COMPLETED, cleanup_done=False)
        for _ in range(MAX_TERMINAL_OP_ATTEMPTS):
            self.ledger.resolve_terminal_op(task_id, success=False)
        self.assertEqual(self.ledger.records(), [])

    def test_reconcile_marks_nonterminal_interrupted(self):
        with_msg = self.ledger.create(1, 2)
        self.ledger.set_status_message(with_msg, 42)
        self.ledger.create(1, 3)  # no status message → purged outright
        count = self.ledger.reconcile_interrupted()
        self.assertEqual(count, 2)
        rec = self._only_record()
        self.assertEqual(rec["state"], INTERRUPTED)
        self.assertEqual(rec["terminal_op"]["message_id"], 42)
        # idempotent: second reconcile finds nothing non-terminal
        self.assertEqual(self.ledger.reconcile_interrupted(), 0)

    def test_corrupt_file_fails_open(self):
        path = Path(self._td.name) / "sub" / "tasks.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ not json", encoding="utf-8")
        self.assertEqual(self.ledger.records(), [])
        task_id = self.ledger.create(1, 2)  # recovers by overwriting
        self.assertEqual(self._only_record()["task_id"], task_id)

    def test_path_resolution(self):
        override = Path("/tmp/custom-tasks.json")
        self.assertEqual(ledger_path_for(Path("/data"), override), override)
        self.assertEqual(
            ledger_path_for(Path("/data")), default_task_ledger_path(Path("/data"))
        )
        self.assertIsNone(ledger_path_for(None))


if __name__ == "__main__":
    unittest.main()
