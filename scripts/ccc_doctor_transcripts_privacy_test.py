#!/usr/bin/env python3
"""Hermetic verdict tests for doctor's Claude transcripts-dir checks (#2306).

The distill snapshot refuses a group/other-accessible transcripts directory, and
every refused job terminalized as ``invalid_snapshot_request`` with no log line:
two nodes silently lost days of memory (0775/0755 dirs from the umask). These
tests pin that the doctor reports the served directory's mode and the recent
refusal count, and that it never changes anything it inspects.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import json
import os
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ccc_doctor import Doctor, claude_project_slug  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
DIR_ITEM = "claude transcripts dir"
REFUSAL_ITEM = "distill snapshot refusals"


class TranscriptsPrivacyCheck(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        self.base = Path(self._temp.name).resolve()
        self.claude_dir = self.base / ".claude"
        (self.claude_dir / "projects").mkdir(parents=True)
        self.served = self.claude_dir / "projects" / claude_project_slug(self.base)
        self.env = {"CCC_DOCTOR_BRIDGE_PROJECT_ROOT": str(self.base), "PATH": os.environ.get("PATH", "")}

    def tearDown(self) -> None:
        self._temp.cleanup()

    def run_checks(self) -> Doctor:
        doctor = Doctor(REPO, self.claude_dir, "settings")
        with patch.dict("os.environ", self.env, clear=True):
            doctor.check_claude_transcripts_privacy()
            doctor.check_snapshot_refusals()
        return doctor

    def row(self, doctor: Doctor, item: str):
        rows = [r for r in doctor.rows if r.item == item]
        self.assertEqual(len(rows), 1, [r.item for r in doctor.rows])
        return rows[0]

    def test_slug_matches_bridge_rule(self) -> None:
        self.assertEqual(
            claude_project_slug(Path("/data/data/com.termux/files/home")),
            "-data-data-com-termux-files-home",
        )
        self.assertEqual(claude_project_slug(Path("/opt/ccc_node")), "-opt-ccc-node")

    def test_missing_dir_is_not_applicable(self) -> None:
        row = self.row(self.run_checks(), DIR_ITEM)
        self.assertEqual(row.klass, "정상")
        self.assertIn("해당 없음", row.status)

    def test_private_dir_is_ok(self) -> None:
        self.served.mkdir(mode=0o700)
        row = self.row(self.run_checks(), DIR_ITEM)
        self.assertEqual(row.klass, "정상")
        self.assertIn("mode=0700", row.status)

    def test_umask_dir_warns_and_is_not_modified(self) -> None:
        for mode in (0o755, 0o775):
            self.served.mkdir(mode=0o700, exist_ok=True)
            self.served.chmod(mode)
            row = self.row(self.run_checks(), DIR_ITEM)
            self.assertEqual(row.klass, "경고")
            self.assertIn(f"mode={mode:04o}", row.status)
            self.assertIn("chmod 700", row.action)
            self.assertEqual(self.served.stat().st_mode & 0o777, mode)

    def test_symlinked_dir_needs_manual_fix(self) -> None:
        real = self.base / "elsewhere"
        real.mkdir(mode=0o700)
        self.served.symlink_to(real)
        row = self.row(self.run_checks(), DIR_ITEM)
        self.assertEqual(row.klass, "수동필요")
        self.assertIn("not a real directory", row.status)

    def test_foreign_owner_needs_manual_fix(self) -> None:
        self.served.mkdir(mode=0o700)
        with patch("ccc_doctor.os.geteuid", return_value=os.geteuid() + 1):
            row = self.row(self.run_checks(), DIR_ITEM)
        self.assertEqual(row.klass, "수동필요")
        self.assertIn("owner uid=", row.status)

    def _job(self, journal: Path, name: str, payload: object, *, age_days: float = 0) -> None:
        journal.mkdir(parents=True, exist_ok=True)
        path = journal / f"{name}.json"
        path.write_text(payload if isinstance(payload, str) else json.dumps(payload))
        stamp = time.time() - age_days * 86400
        os.utime(path, (stamp, stamp))

    def test_no_journals_reports_zero(self) -> None:
        row = self.row(self.run_checks(), REFUSAL_ITEM)
        self.assertEqual(row.klass, "정상")
        self.assertIn("recent=0", row.status)

    def test_recent_refusals_warn_old_and_other_errors_ignored(self) -> None:
        matrix = self.base / ".ccc-matrix" / "distill-journal"
        telegram = self.base / ".telegram_bot" / "distill-journal"
        refused = {"error_code": "invalid_snapshot_request", "updated_at": "2026-10-14T02:40:00Z"}
        self._job(matrix, "a", refused)
        self._job(matrix, "b", {**refused, "updated_at": "2026-10-13T19:45:00Z"})
        self._job(telegram, "c", {"error_code": "max_attempts_exceeded"})
        self._job(telegram, "d", refused, age_days=8)
        self._job(matrix, "e", "{not json")
        row = self.row(self.run_checks(), REFUSAL_ITEM)
        self.assertEqual(row.klass, "경고")
        self.assertIn("invalid_snapshot_request=2 in 7d", row.status)
        self.assertIn("latest 2026-10-14T02:40", row.status)
        self.assertIn("ccc-matrix", row.status)
        self.assertNotIn("telegram", row.status)
        self.assertIn("unreadable=1", row.status)
        self.assertIn("explicit approval", row.action)

    def test_danso_journal_counted_and_action_says_it_clears(self) -> None:
        journal = self.base / ".ccc-matrix" / "danso-distill-journal"
        self._job(journal, "a", {"error_code": "invalid_snapshot_request", "created_at": "2026-10-07T13:49:00Z"})
        row = self.row(self.run_checks(), REFUSAL_ITEM)
        self.assertEqual(row.klass, "경고")
        self.assertIn("invalid_snapshot_request=1", row.status)
        self.assertIn("clears on its own", row.action)

    def test_unreadable_journal_dir_is_not_reported_as_zero(self) -> None:
        journal = self.base / ".ccc-matrix" / "distill-journal"
        self._job(journal, "a", {"error_code": "invalid_snapshot_request"})
        journal.chmod(0)
        try:
            if os.access(journal, os.R_OK):
                self.skipTest("running with read access to mode-0 dirs (root)")
            row = self.row(self.run_checks(), REFUSAL_ITEM)
        finally:
            journal.chmod(0o700)
        self.assertEqual(row.klass, "경고")
        self.assertIn("unreadable=1", row.status)
        self.assertIn("bridge user", row.action)

    def test_only_other_errors_is_ok(self) -> None:
        journal = self.base / ".ccc-matrix" / "distill-journal"
        self._job(journal, "a", {"error_code": "snapshot_read_failed"})
        self._job(journal, "b", {"status": "extraction_done", "error_code": None})
        row = self.row(self.run_checks(), REFUSAL_ITEM)
        self.assertEqual(row.klass, "정상")
        self.assertIn("scanned=2", row.status)


if __name__ == "__main__":
    unittest.main()
