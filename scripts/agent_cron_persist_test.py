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
import sys
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
            "commit_run_state", "release_for_run", "notification_base",
            "headless_metadata", "mutation_flags", "parse_schedule",
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
        agent_cron.release_for_run = lambda *a, **k: {"ok": True, "state": "released"}
        agent_cron.notification_base = lambda t: {"policy": "none"}
        agent_cron.headless_metadata = lambda t, execute=False: {"command": "true"}
        agent_cron.mutation_flags = lambda *a, **k: {}
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

    def test_lock_is_still_released_when_persist_fails(self) -> None:
        released = []
        agent_cron.release_for_run = lambda *a, **k: (
            released.append(a) or {"ok": True, "state": "released"}
        )
        agent_cron.run_execute({}, "probe", None, True)
        self.assertEqual(len(released), 1, "the run lock must not leak on a persist failure")


if __name__ == "__main__":
    unittest.main(verbosity=1)
