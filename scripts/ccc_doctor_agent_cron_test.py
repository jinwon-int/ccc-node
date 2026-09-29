#!/usr/bin/env python3
"""Hermetic verdict tests for doctor's stale prompt-task success check (#1821).

The 2026-09 incident: a node's claude login broke and prompt tasks failed for
six days while every other signal stayed green. "The last prompt success is
older than D days" was the earliest signal available, so doctor reads it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ccc_doctor import Doctor  # noqa: E402

ITEM = "agent-cron prompt success"


def stamp(days_ago: float) -> str:
    moment = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def run(status: str, days_ago: float) -> dict:
    return {"runId": f"r-{days_ago}", "scheduledAt": stamp(days_ago), "startedAt": stamp(days_ago),
            "status": status, "attempt": 1, "notifyState": "none"}


def task(task_id: str, history: list[dict], **extra) -> dict:
    return {"id": task_id, "schedule": "0 9 * * *", "prompt": "p", "enabled": True,
            "runHistory": history, **extra}


class AgentCronPromptSuccessCheck(unittest.TestCase):
    def check(self, tasks: list[dict] | None, env: dict[str, str] | None = None,
              raw: str | None = None, alarm: dict | None = None) -> Doctor:
        with TemporaryDirectory() as temp:
            claude_dir = Path(temp) / ".claude"
            store = claude_dir / "state" / "agent-cron" / "tasks.json"
            store.parent.mkdir(parents=True)
            if raw is not None:
                store.write_text(raw, encoding="utf-8")
            elif tasks is not None:
                store.write_text(json.dumps({"version": 1, "tasks": tasks}), encoding="utf-8")
            if alarm is not None:
                (store.parent / "failure-alarm.json").write_text(
                    json.dumps({"version": 2, "tasks": alarm, "node": {}}), encoding="utf-8")
            doctor = Doctor(Path.cwd(), claude_dir, "settings")
            with patch.dict("os.environ", env or {}, clear=True):
                doctor.check_agent_cron_prompt_success()
        row = doctor.rows[-1]
        self.assertEqual(row.item, ITEM)
        return doctor

    def test_recurring_task_stale_warns_with_class_from_alarm_state(self) -> None:
        doctor = self.check(
            [task("observe", [run("success", 45), run("failed", 8), run("failed", 2)])],
            alarm={"observe": {"failureClass": "auth_failed"}})
        row = doctor.rows[-1]
        self.assertEqual(row.klass, "경고")
        self.assertIn("observe(last-success=45d,class=auth_failed)", row.status)
        self.assertIn("claude login", row.action)

    def test_incident_one_shot_shape_warns_at_node_level(self) -> None:
        # Four different one-shot tasks each failed once; last success 44d ago.
        tasks = [task("old-ok", [run("success", 44)], schedule="at 2026-08-05T00:00:00Z")]
        tasks += [task(f"observe-{n}", [run("failed", age)], schedule="at 2026-09-12T00:00:00Z")
                  for n, age in enumerate((7.5, 6.5, 4.5, 0.5))]
        row = self.check(tasks).rows[-1]
        self.assertEqual(row.klass, "경고")
        self.assertIn("node-prompt(last-success=44d)", row.status)
        self.assertEqual(row.status.count("observe-"), 0)  # one-shots: no per-task rows

    def test_one_shot_failure_does_not_warn_forever(self) -> None:
        # Nothing has been attempted for > D days: the node verdict clears.
        tasks = [task("once", [run("failed", 30)], schedule="at 2026-08-30T00:00:00Z")]
        self.assertEqual(self.check(tasks).rows[-1].klass, "정상")

    def test_never_succeeded_recurring_task_warns_after_d_days(self) -> None:
        doctor = self.check([task("daily", [run("failed", 8), run("failed", 1)])])
        self.assertEqual(doctor.rows[-1].klass, "경고")
        self.assertIn("daily(no-success-since-first-run=8d,class=unclassified)", doctor.rows[-1].status)

    def test_recent_failure_within_window_is_ok(self) -> None:
        doctor = self.check([task("observe", [run("success", 3), run("failed", 1)])])
        self.assertEqual(doctor.rows[-1].klass, "정상")

    def test_latest_success_is_ok_even_if_old(self) -> None:
        doctor = self.check([task("weekly", [run("failed", 40), run("success", 30)])])
        self.assertEqual(doctor.rows[-1].klass, "정상")

    def test_alarm_state_last_success_outlives_bounded_history(self) -> None:
        # runHistory holds only failures; failure-alarm.json says 2 days ago.
        doctor = self.check([task("observe", [run("failed", 9), run("failed", 1)])],
                            alarm={"observe": {"lastSuccessAt": stamp(2)}})
        self.assertEqual(doctor.rows[-1].klass, "정상")

    def test_disabled_and_command_tasks_are_skipped(self) -> None:
        doctor = self.check([
            task("off", [run("failed", 30)], enabled=False),
            task("cmd", [run("failed", 3)], payload={"kind": "command", "argv": ["true"]}),
        ])
        self.assertEqual(doctor.rows[-1].klass, "정상")
        self.assertIn("stale=0", doctor.rows[-1].status)

    def test_threshold_is_configurable(self) -> None:
        tasks = [task("observe", [run("success", 5), run("failed", 1)])]
        self.assertEqual(self.check(tasks).rows[-1].klass, "정상")
        doctor = self.check(tasks, {"CCC_DOCTOR_AGENT_CRON_STALE_DAYS": "3"})
        self.assertEqual(doctor.rows[-1].klass, "경고")
        self.assertIn("threshold=3d", doctor.rows[-1].status)

    def test_absent_and_unreadable_store(self) -> None:
        self.assertEqual(self.check(None).rows[-1].status, "store=absent")
        doctor = self.check(None, raw="{not json")
        self.assertEqual(doctor.rows[-1].klass, "경고")
        self.assertEqual(doctor.rows[-1].status, "store=unreadable")

    def test_store_resolution_matches_agent_cron(self) -> None:
        with TemporaryDirectory() as temp:
            store = Path(temp) / "custom.json"
            store.write_text(json.dumps({"version": 1, "tasks": [
                task("observe", [run("failed", 10)])]}), encoding="utf-8")
            doctor = self.check([], {"CCC_AGENT_CRON_STORE": str(store)})
            self.assertEqual(doctor.rows[-1].klass, "경고")
            # agent_cron.py ignores CCC_STATE_DIR, so doctor must too.
            doctor = self.check([], {"CCC_STATE_DIR": str(Path(temp))})
            self.assertEqual(doctor.rows[-1].klass, "정상")


if __name__ == "__main__":
    unittest.main()
