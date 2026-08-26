#!/usr/bin/env python3
"""scheduler_execute must reuse its due plan instead of replanning per task.

run_execute() used to call run_plan_for() -- a full all-tasks due_plan() -- for
every executed task even though scheduler_execute() already held the plan and
its rows. The scheduler fires every minute, so the handed-over row must produce
a byte-identical result while due_plan() runs once per tick, and the manual
`run` path (no row handed over) must keep planning for itself. Also covers the
boot_id() process cache introduced for the same per-plan cost reason.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent_cron


class PlanRowReuseTest(unittest.TestCase):
    def setUp(self):
        self._saved = {}
        for name in (
            "task_by_id", "run_plan_for", "run_limit_metadata", "acquire_for_run",
            "run_headless", "write_owner_spool", "history_attempt",
            "append_run_history", "apply_retry_transition", "apply_run_limit",
            "commit_run_state", "release_for_run", "notification_base",
            "headless_metadata", "parse_schedule",
        ):
            self._saved[name] = getattr(agent_cron, name)

        self.task = {"id": "probe", "enabled": True, "notify": "none", "keepAfterRun": True}
        self.plan = {
            "at": "2026-01-01T00:05:00Z",
            "ok": True,
            "tasks": [{"id": "probe", "due": True,
                       "scheduledAt": "2026-01-01T00:05:00Z", "status": "due"}],
        }
        self.replans = 0

        def fake_run_plan_for(data, task_id, at_value):
            self.replans += 1
            row = next((t for t in self.plan["tasks"] if t.get("id") == task_id), None)
            return self.plan, row

        agent_cron.task_by_id = lambda data, tid: self.task if tid == "probe" else None
        agent_cron.run_plan_for = fake_run_plan_for
        agent_cron.run_limit_metadata = lambda t: {"reached": False}
        agent_cron.acquire_for_run = lambda *a, **k: (True, {"state": "acquired", "path": "/tmp/x"})
        agent_cron.run_headless = lambda t: {"exitCode": 0, "stdout": "", "stderr": ""}
        agent_cron.write_owner_spool = lambda *a, **k: {"delivery": "none"}
        agent_cron.history_attempt = lambda *a, **k: 1
        agent_cron.append_run_history = lambda *a, **k: None
        agent_cron.apply_retry_transition = lambda *a, **k: {}
        agent_cron.apply_run_limit = lambda t: {"reached": False}
        agent_cron.commit_run_state = lambda *a, **k: True
        agent_cron.release_for_run = lambda *a, **k: {"ok": True, "state": "released"}
        agent_cron.notification_base = lambda t: {"policy": "none"}
        agent_cron.headless_metadata = lambda t, execute=False: {"command": "true"}
        agent_cron.parse_schedule = lambda *a, **k: {"kind": "cron"}

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(agent_cron, name, value)

    def test_supplied_row_skips_replan_and_result_is_identical(self):
        baseline, _as_json, rc = agent_cron.run_execute({}, "probe", None, True)
        self.assertEqual(rc, 0)
        self.assertEqual(self.replans, 1)
        fast, _as_json, rc = agent_cron.run_execute(
            {}, "probe", None, True,
            plan_row=self.plan["tasks"][0], plan_at=self.plan["at"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.replans, 1)  # the handed-over row skipped run_plan_for
        self.assertEqual(baseline, fast)

    def test_partial_hint_still_replans(self):
        # A row without the plan's at (or vice versa) cannot pin the clock, so
        # run_execute must fall back to planning for itself.
        _result, _as_json, rc = agent_cron.run_execute(
            {}, "probe", None, True, plan_row=self.plan["tasks"][0])
        self.assertEqual(rc, 0)
        self.assertEqual(self.replans, 1)
        _result, _as_json, rc = agent_cron.run_execute(
            {}, "probe", None, True, plan_at=self.plan["at"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.replans, 2)


class SchedulerExecuteWiringTest(unittest.TestCase):
    def test_scheduler_execute_passes_each_precomputed_row(self):
        saved = agent_cron.run_execute
        calls = []

        def spy(data, task_id, at_value, as_json, plan_row=None, plan_at=None):
            calls.append((task_id, plan_row, plan_at))
            return {"mutations": {}}, as_json, 0

        agent_cron.run_execute = spy
        try:
            row_a = {"id": "a", "due": True, "scheduledAt": "2026-01-01T00:05:00Z"}
            row_b = {"id": "b", "due": True, "scheduledAt": "2026-01-01T00:05:00Z"}
            plan = {"at": "2026-01-01T00:05:00Z", "ok": True, "errors": [],
                    "tasks": [row_a, row_b]}
            actions = [
                {"taskId": "a", "action": "would-run"},
                {"taskId": "b", "action": "would-run"},
                {"taskId": "c", "action": "skip"},
            ]
            result, _as_json, rc = agent_cron.scheduler_execute({}, plan, actions, "", True, 10)
        finally:
            agent_cron.run_execute = saved
        self.assertEqual(rc, 0)
        self.assertEqual(result["executedActions"], 2)
        self.assertEqual([c[0] for c in calls], ["a", "b"])
        self.assertIs(calls[0][1], row_a)  # the plan's own row object, not a recomputation
        self.assertIs(calls[1][1], row_b)
        self.assertEqual(calls[0][2], plan["at"])
        self.assertEqual(calls[1][2], plan["at"])


class BootIdCacheTest(unittest.TestCase):
    def test_boot_id_reads_proc_once_per_process(self):
        saved_path = agent_cron.Path
        saved_cache = agent_cron._boot_id_cache
        reads = []

        class SpyPath:
            def __init__(self, raw):
                self._raw = raw

            def read_text(self, encoding=None):
                reads.append(self._raw)
                return "boot-1234\n"

        agent_cron.Path = SpyPath
        agent_cron._boot_id_cache = None
        try:
            self.assertEqual(agent_cron.boot_id(), "boot-1234")
            self.assertEqual(agent_cron.boot_id(), "boot-1234")
        finally:
            agent_cron.Path = saved_path
            agent_cron._boot_id_cache = saved_cache
        self.assertEqual(reads, ["/proc/sys/kernel/random/boot_id"])


if __name__ == "__main__":
    unittest.main()
