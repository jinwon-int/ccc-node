#!/usr/bin/env python3
"""run_execute must not let a persist failure masquerade as a dry run (#869).

commit_run_state() runs AFTER the headless work and the owner notification.
When it threw, the exception escaped run_execute() and was swallowed by
run_dry_plan()'s blanket handler, which reported
``{"mode": "run-dry-run-read-only", "taskId": None}`` -- an executed, notified
run recorded as a read-only dry run. lastRunAt/runCount stayed unwritten, so
the next scheduler tick saw the same occurrence as never-run and executed
(and notified) it again.

A black-box trigger is not viable: the natural way to make the persist fail is
an unwritable store directory, and the scheduler commonly runs as root, which
bypasses directory permission checks entirely.
"""

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent_cron


class PersistFailureTest(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {}
        for name in (
            "task_by_id", "run_plan_for", "run_limit_metadata", "acquire_for_run",
            "run_headless", "write_owner_spool", "history_attempt",
            "append_run_history", "apply_retry_transition", "apply_run_limit",
            "commit_run_state", "release_for_run", "quarantine_persist_failure",
            "notification_base", "headless_metadata", "parse_schedule",
        ):
            self._saved[name] = getattr(agent_cron, name)

        task = {"id": "probe", "enabled": True, "notify": "none", "keepAfterRun": True}
        agent_cron.task_by_id = lambda data, tid: task
        agent_cron.run_plan_for = lambda data, tid, at: (
            {"at": "2026-01-01T00:00:00Z", "ok": True},
            {"due": True, "scheduledAt": "2026-01-01T00:00:00Z"},
        )
        agent_cron.run_limit_metadata = lambda t: {"reached": False}
        agent_cron.acquire_for_run = lambda *a, **k: (True, {"state": "acquired", "path": "/tmp/x"})
        agent_cron.run_headless = lambda t: {"exitCode": 0, "stdout": "", "stderr": ""}
        agent_cron.write_owner_spool = lambda *a, **k: {"delivery": "none"}
        agent_cron.history_attempt = lambda *a, **k: 1
        agent_cron.append_run_history = lambda *a, **k: None
        agent_cron.apply_retry_transition = lambda *a, **k: {}
        agent_cron.apply_run_limit = lambda t: {"reached": False}
        self.released = []
        self.quarantined = []
        agent_cron.release_for_run = lambda *a, **k: (
            self.released.append(a) or {"ok": True, "state": "released"}
        )
        agent_cron.quarantine_persist_failure = lambda *a, **k: (
            self.quarantined.append(a)
            or {"ok": True, "state": "persist-failed", "path": "/tmp/x"}
        )
        agent_cron.notification_base = lambda t: {"policy": "none"}
        agent_cron.headless_metadata = lambda t, execute=False: {"command": "true"}
        agent_cron.parse_schedule = lambda *a, **k: {"kind": "cron"}

        def boom(*_a, **_k):
            raise OSError("store is unwritable")

        agent_cron.commit_run_state = boom

    def tearDown(self) -> None:
        for name, value in self._saved.items():
            setattr(agent_cron, name, value)

    def test_persist_failure_does_not_escape_run_execute(self) -> None:
        result, _as_json, rc = agent_cron.run_execute({}, "probe", None, True)
        # The run really happened: it must be reported against its real mode,
        # not rewritten into a phantom dry run by the caller's handler.
        self.assertEqual(result["mode"], "run-execute")
        self.assertEqual(result["taskId"], "probe")
        # And it must be loud, so the operator repairs the store instead of
        # letting the next tick re-execute the same occurrence.
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "persist-failed")
        self.assertIn("unwritable", result["persistError"])
        self.assertNotEqual(rc, 0)
        self.assertFalse(result["mutations"]["taskStoreWrite"])
        self.assertFalse(result["mutations"]["historyAppend"])
        self.assertEqual(result["lock"]["release"]["state"], "persist-failed")

    def test_lock_is_quarantined_instead_of_released(self) -> None:
        agent_cron.run_execute({}, "probe", None, True)
        self.assertEqual(self.released, [])
        self.assertEqual(len(self.quarantined), 1)


class PersistQuarantineLockTest(unittest.TestCase):
    def setUp(self) -> None:
        self._old_store = agent_cron.store
        self.tempdir = tempfile.TemporaryDirectory()
        agent_cron.store = Path(self.tempdir.name) / "tasks.json"

    def tearDown(self) -> None:
        agent_cron.store = self._old_store
        self.tempdir.cleanup()

    def test_quarantine_blocks_future_ticks_until_exact_release(self) -> None:
        at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        task = {"id": "probe", "lockTimeoutSec": 1}
        acquired, lock = agent_cron.acquire_for_run(
            "probe", task, "run-1", "2026-01-01T00:00:00Z", at
        )
        self.assertTrue(acquired)

        quarantine = agent_cron.quarantine_persist_failure("probe", "run-1", at)
        self.assertTrue(quarantine["ok"])
        self.assertEqual(quarantine["state"], "persist-failed")

        # The ordinary one-second timeout and even a later scheduler tick must
        # not reclaim a run whose durable completion record was never written.
        later = at + timedelta(days=1)
        status = agent_cron.lock_status("probe", task, later)
        self.assertEqual(status["lockState"], "persist-failed")
        acquired_again, blocked = agent_cron.acquire_for_run(
            "probe", task, "run-2", "2026-01-01T00:00:00Z", later
        )
        self.assertFalse(acquired_again)
        self.assertEqual(blocked["state"], "persist-failed")

        actions = agent_cron.scheduler_actions({
            "tasks": [{
                "id": "probe",
                "enabled": True,
                "due": True,
                "status": "persist-failed",
                "lockState": "persist-failed",
            }]
        })
        self.assertEqual(actions[0]["action"], "skip")
        self.assertEqual(actions[0]["reason"], "persist-failed")

        # Recovery is explicit and run-id bound; the normal release primitive
        # remains the operator's way to clear the quarantine after repair.
        released = agent_cron.release_for_run("probe", lock["holder"]["runId"])
        self.assertEqual(released["state"], "released")
        self.assertEqual(agent_cron.lock_status("probe", task, later)["lockState"], "free")


if __name__ == "__main__":
    unittest.main(verbosity=1)
