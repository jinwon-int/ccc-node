#!/usr/bin/env python3
"""Hermetic verdict tests for doctor's skill-autosave pending-backlog check (#2184).

The human gate keeps every autosave draft until `/skillsuggest` decides it;
the nightly sweep now expires drafts past 90 days into the archive root.
This check warns at 60 days so the owner sees an unreviewed queue before
the expiry discards work nobody looked at. These tests pin: an absent queue
is not drift, a young queue is healthy, decided/approved/proposal entries
do not count, the verdict reports the oldest age and the expiry setting,
and age falls back from meta.json to the directory stamp.
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


class PendingBacklogCheck(unittest.TestCase):
    def run_check(self, drafts: list[tuple[str, float | None, str | None]] | None, env: dict[str, str] | None = None) -> Doctor:
        """drafts: (dir name, staged_at epoch or None for no meta, extra marker file or None)."""
        with TemporaryDirectory() as temp:
            claude_dir = Path(temp) / ".claude"
            state = claude_dir / "state"
            state.mkdir(parents=True)
            if drafts is not None:
                pending = state / "pending-skills"
                pending.mkdir()
                for name, staged, marker in drafts:
                    d = pending / name
                    d.mkdir()
                    (d / "SKILL.md").write_text("# x\n", encoding="utf-8")
                    if staged is not None:
                        (d / "meta.json").write_text(json.dumps({"staged_at": iso(staged)}), encoding="utf-8")
                    if marker:
                        (d / marker).write_text("{}", encoding="utf-8")
            doctor = Doctor.__new__(Doctor)
            doctor.claude_dir = claude_dir
            doctor.rows = []
            doctor.counts = {"정상": 0, "경고": 0, "수동필요": 0, "자동수정가능": 0}
            merged = {"CCC_STATE_DIR": str(state), **(env or {})}
            with patch.dict(os.environ, merged):
                os.environ.pop("CCC_SKILL_PENDING_EXPIRE_DAYS", None)
                if env and "CCC_SKILL_PENDING_EXPIRE_DAYS" in env:
                    os.environ["CCC_SKILL_PENDING_EXPIRE_DAYS"] = env["CCC_SKILL_PENDING_EXPIRE_DAYS"]
                doctor.check_skill_pending_backlog()
            return doctor

    def row_for(self, doctor: Doctor):
        rows = [r for r in doctor.rows if r.item == "skill-autosave pending backlog"]
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_absent_queue_is_not_drift(self) -> None:
        row = self.row_for(self.run_check(None))
        self.assertEqual(row.klass, "정상")
        self.assertEqual(row.status, "queue=absent")

    def test_young_queue_is_healthy(self) -> None:
        now = time.time()
        row = self.row_for(self.run_check([("20261001-000000-aaaa-x", now - 3 * DAY, None)]))
        self.assertEqual(row.klass, "정상")
        self.assertTrue(row.status.startswith("undecided=1; over60d=0; oldest=3d; expire=90d"), row.status)

    def test_old_draft_warns_with_oldest_age(self) -> None:
        now = time.time()
        drafts = [
            ("20260601-000000-aaaa-old", now - 100 * DAY, None),
            ("20260801-000000-bbbb-mid", now - 61 * DAY, None),
            ("20261001-000000-cccc-new", now - 2 * DAY, None),
        ]
        row = self.row_for(self.run_check(drafts))
        self.assertEqual(row.klass, "경고")
        self.assertEqual(row.status, "undecided=3; over60d=2; oldest=100d; expire=90d")
        self.assertIn("/skillsuggest", row.action)
        self.assertIn("pending-expire --dry-run", row.action)

    def test_decided_and_approved_and_proposal_do_not_count(self) -> None:
        now = time.time()
        drafts = [
            ("20260601-000000-aaaa-done.approved-20260603120000", now - 100 * DAY, None),
            ("20260601-000000-bbbb-done.installed-20260603120000", now - 100 * DAY, None),
            ("20260601-000000-cccc-wait", now - 100 * DAY, "meta.approved.json"),
            ("20260601-000000-dddd-prop", now - 100 * DAY, "proposal.json"),
        ]
        row = self.row_for(self.run_check(drafts))
        self.assertEqual(row.klass, "정상")
        self.assertEqual(row.status, "undecided=0; expire=90d")

    def test_age_falls_back_to_directory_stamp(self) -> None:
        # No meta.json: the YYYYMMDD-HHMMSS prefix dates the draft (2026-06-01 = old).
        row = self.row_for(self.run_check([("20260601-000000-aaaa-stamp", None, None)]))
        self.assertEqual(row.klass, "경고")
        self.assertTrue(row.status.startswith("undecided=1; over60d=1;"), row.status)

    def test_expiry_setting_is_reported(self) -> None:
        now = time.time()
        row = self.row_for(self.run_check([("20261001-000000-aaaa-x", now - DAY, None)], {"CCC_SKILL_PENDING_EXPIRE_DAYS": "0"}))
        self.assertTrue(row.status.endswith("expire=offd"), row.status)
        row = self.row_for(self.run_check([("20261001-000000-aaaa-x", now - DAY, None)], {"CCC_SKILL_PENDING_EXPIRE_DAYS": "30"}))
        self.assertTrue(row.status.endswith("expire=30d"), row.status)


if __name__ == "__main__":
    unittest.main()
