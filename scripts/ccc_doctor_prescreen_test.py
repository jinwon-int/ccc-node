#!/usr/bin/env python3
"""Hermetic verdict tests for doctor's skill-autosave prescreen check (#2183).

`prescreen.py` leaves `state/prescreen-last.json` after each run. The check
must treat a node that never ran it as healthy, flag a reviewer that is down
or unavailable, flag a run where every attempt errored, flag a stale run
while a queue exists, and otherwise report the last run's counts.
"""

from __future__ import annotations

import json
import os
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ccc_doctor import Doctor  # noqa: E402

DAY = 86400


def iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


class PrescreenCheck(unittest.TestCase):
    def run_check(self, last: dict | str | None, *, queue: bool = True) -> Doctor:
        with TemporaryDirectory() as temp:
            claude_dir = Path(temp) / ".claude"
            state = claude_dir / "state"
            state.mkdir(parents=True)
            if queue:
                (state / "pending-skills").mkdir()
            if isinstance(last, dict):
                (state / "prescreen-last.json").write_text(json.dumps(last), encoding="utf-8")
            elif isinstance(last, str):
                (state / "prescreen-last.json").write_text(last, encoding="utf-8")
            doctor = Doctor.__new__(Doctor)
            doctor.claude_dir = claude_dir
            doctor.rows = []
            doctor.counts = {"정상": 0, "경고": 0, "수동필요": 0, "자동수정가능": 0}
            with patch.dict(os.environ, {"CCC_STATE_DIR": str(state)}):
                doctor.check_skill_prescreen()
            return doctor

    def row_for(self, doctor: Doctor):
        rows = [r for r in doctor.rows if r.item == "skill-autosave prescreen"]
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_never_ran_is_healthy(self) -> None:
        row = self.row_for(self.run_check(None))
        self.assertEqual(row.klass, "정상")
        self.assertEqual(row.status, "last=never; queue=present")

    def test_recent_clean_run_is_healthy(self) -> None:
        last = {"ts": iso(time.time() - 3600), "status": "reviewed", "reviewed": 5, "archived": ["a"], "errors": 0}
        row = self.row_for(self.run_check(last))
        self.assertEqual(row.klass, "정상")
        self.assertEqual(row.status, "last=0d status=reviewed reviewed=5 archived=1 errors=0")

    def test_reviewer_down_warns(self) -> None:
        last = {"ts": iso(time.time() - 3600), "status": "reviewer-down", "reviewed": 0, "archived": [], "errors": 3}
        row = self.row_for(self.run_check(last))
        self.assertEqual(row.klass, "경고")
        self.assertIn("REVIEW_AGENT_BIN", row.action)

    def test_all_errors_warns(self) -> None:
        last = {"ts": iso(time.time() - 3600), "status": "reviewed", "reviewed": 0, "archived": [], "errors": 2}
        row = self.row_for(self.run_check(last))
        self.assertEqual(row.klass, "경고")

    def test_stale_run_with_queue_warns(self) -> None:
        last = {"ts": iso(time.time() - 4 * DAY), "status": "reviewed", "reviewed": 2, "archived": [], "errors": 0}
        row = self.row_for(self.run_check(last))
        self.assertEqual(row.klass, "경고")
        self.assertIn("3+ days", row.action)

    def test_stale_run_without_queue_is_healthy(self) -> None:
        last = {"ts": iso(time.time() - 10 * DAY), "status": "reviewed", "reviewed": 2, "archived": [], "errors": 0}
        row = self.row_for(self.run_check(last, queue=False))
        self.assertEqual(row.klass, "정상")

    def test_malformed_last_needs_manual(self) -> None:
        row = self.row_for(self.run_check("{not json"))
        self.assertEqual(row.klass, "수동필요")


if __name__ == "__main__":
    unittest.main()
