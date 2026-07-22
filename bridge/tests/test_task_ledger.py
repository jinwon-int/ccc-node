"""Unit tests for the persistent task ledger (Hermes-style task lifecycle)."""

import json
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
    WAITING_FOR_TURN,
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
        self.ledger.set_state(task_id, WAITING_FOR_TURN)
        self.assertEqual(self._only_record()["state"], WAITING_FOR_TURN)
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


class TaskLedgerDurabilityTests(unittest.TestCase):
    """#443: tasks.json must carry the same durability guarantees as sessions.json
    — previous-good .bak retention, crash recovery, and atomic (temp+replace)
    writes with no fixed-name .tmp left behind."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.path = Path(self._td.name) / "sub" / "tasks.json"
        self.ledger = TaskLedger(self.path)

    def _bak(self) -> Path:
        return self.path.with_name(self.path.name + ".bak")

    def test_backup_written_before_overwrite(self):
        # First write creates the primary (no prior file to back up).
        task_id = self.ledger.create(1, 2)
        self.assertFalse(self._bak().exists())
        # Second write preserves the current-good file as .bak first.
        self.ledger.set_status_message(task_id, 777)
        self.assertTrue(self._bak().exists())
        backed = json.loads(self._bak().read_text(encoding="utf-8"))
        # .bak holds the pre-second-write state (status message still unset).
        self.assertIn(task_id, backed)
        self.assertIsNone(backed[task_id]["status_message_id"])
        # Primary holds the new state.
        primary = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(primary[task_id]["status_message_id"], 777)

    def test_recovers_from_backup_when_primary_corrupt(self):
        task_id = self.ledger.create(1, 2)
        self.ledger.set_status_message(task_id, 777)  # ensures a .bak exists
        self.assertTrue(self._bak().exists())
        # Simulate a damaged primary (disk error / external truncation).
        self.path.write_text("{ half-written garbage", encoding="utf-8")
        # A fresh instance must recover the task from the previous-good backup
        # instead of losing it (parity with SessionStore crash recovery).
        recovered = TaskLedger(self.path)
        ids = [r["task_id"] for r in recovered.records()]
        self.assertIn(task_id, ids)

    def test_no_fixed_tmp_or_leftover_temp_after_write(self):
        task_id = self.ledger.create(1, 2)
        self.ledger.set_status_message(task_id, 42)
        self.ledger.finish(task_id, COMPLETED, cleanup_done=False)
        names = [p.name for p in self.path.parent.iterdir()]
        # The old fixed-name tasks.json.tmp must never appear …
        self.assertNotIn("tasks.json.tmp", names)
        # … and mkstemp temp files must be cleaned up (only ledger + backup).
        leftover = [n for n in names if ".tmp-" in n]
        self.assertEqual(leftover, [], f"stray temp files: {leftover}")

    def test_state_survives_a_fresh_instance(self):
        task_id = self.ledger.create(5, 6)
        reopened = TaskLedger(self.path)
        self.assertEqual([r["task_id"] for r in reopened.records()], [task_id])


if __name__ == "__main__":
    unittest.main()
