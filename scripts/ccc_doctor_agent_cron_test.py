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


def run(status: str, days_ago: float, failure_class: str | None = None) -> dict:
    entry = {"runId": f"r-{days_ago}", "scheduledAt": stamp(days_ago), "startedAt": stamp(days_ago),
             "status": status, "attempt": 1, "notifyState": "none"}
    if failure_class:
        entry["failureClass"] = failure_class
    return entry


def task(task_id: str, history: list[dict], **extra) -> dict:
    return {"id": task_id, "schedule": "* * * * *", "prompt": "p", "enabled": True,
            "runHistory": history, **extra}


class AgentCronPromptSuccessCheck(unittest.TestCase):
    def check(self, tasks: list[dict] | None, env: dict[str, str] | None = None,
              raw: str | None = None) -> Doctor:
        with TemporaryDirectory() as temp:
            claude_dir = Path(temp) / ".claude"
            store = claude_dir / "state" / "agent-cron" / "tasks.json"
            store.parent.mkdir(parents=True)
            if raw is not None:
                store.write_text(raw, encoding="utf-8")
            elif tasks is not None:
                store.write_text(json.dumps({"version": 1, "tasks": tasks}), encoding="utf-8")
            doctor = Doctor(Path.cwd(), claude_dir, "settings")
            with patch.dict("os.environ", env or {}, clear=True):
                doctor.check_agent_cron_prompt_success()
        row = doctor.rows[-1]
        self.assertEqual(row.item, ITEM)
        return doctor

    def test_incident_shape_warns(self) -> None:
        # Last success 2026-08-05, then only auth failures — stale for weeks.
        doctor = self.check([task("observe", [run("success", 45), run("failed", 8, "auth_failed"),
                                              run("failed", 2, "auth_failed")])])
        row = doctor.rows[-1]
        self.assertEqual(row.klass, "경고")
        self.assertIn("observe(last-success=45d,class=auth_failed)", row.status)
        self.assertIn("re-authenticate", row.action)

    def test_never_succeeded_one_shot_warns_after_d_days(self) -> None:
        doctor = self.check([task("gongmyoung-verify", [run("failed", 8, "auth_failed")])])
        self.assertEqual(doctor.rows[-1].klass, "경고")
        self.assertIn("no-success-since-first-run=8d", doctor.rows[-1].status)

    def test_recent_failure_within_window_is_ok(self) -> None:
        doctor = self.check([task("observe", [run("success", 3), run("failed", 1, "other")])])
        self.assertEqual(doctor.rows[-1].klass, "정상")

    def test_latest_success_is_ok_even_if_old(self) -> None:
        doctor = self.check([task("weekly", [run("failed", 40), run("success", 30)])])
        self.assertEqual(doctor.rows[-1].klass, "정상")

    def test_last_success_at_outlives_bounded_history(self) -> None:
        # runHistory holds only failures, but lastSuccessAt says 2 days ago.
        doctor = self.check([task("observe", [run("failed", 9), run("failed", 1)],
                                  lastSuccessAt=stamp(2))])
        self.assertEqual(doctor.rows[-1].klass, "정상")

    def test_disabled_and_command_tasks_are_skipped(self) -> None:
        doctor = self.check([
            task("off", [run("failed", 30)], enabled=False),
            task("cmd", [run("failed", 30)], payload={"kind": "command", "argv": ["true"]}),
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

    def test_store_env_override(self) -> None:
        with TemporaryDirectory() as temp:
            store = Path(temp) / "custom.json"
            store.write_text(json.dumps({"version": 1, "tasks": [
                task("observe", [run("failed", 10, "cli_missing")])]}), encoding="utf-8")
            doctor = self.check([], {"CCC_AGENT_CRON_STORE": str(store)})
        self.assertEqual(doctor.rows[-1].klass, "경고")
        self.assertIn("class=cli_missing", doctor.rows[-1].status)


if __name__ == "__main__":
    unittest.main()
