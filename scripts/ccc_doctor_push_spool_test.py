#!/usr/bin/env python3
"""Hermetic verdict tests for doctor's push spool dwell check (#2223).

The incident: Telegram push was turned off on a node, its Matrix consumer
drained an overridden ``matrix-spool``, and every cron/hook writer kept the
default ``telegram-spool``. Records sat there for a day while writers reported
``delivery: spooled`` and the bridge health probe (which only counts the dir
its own process consumes) stayed green. These tests pin that the doctor finds
such a dir wherever a writer lane put it, without reading record bodies.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import os
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ccc_doctor import Doctor  # noqa: E402

ITEM = "push spool dwell"


def record(spool: Path, name: str, age_minutes: float) -> Path:
    spool.mkdir(parents=True, exist_ok=True)
    path = spool / name
    path.write_text('{"event":"SelfUpdate"}', encoding="utf-8")
    stamp = time.time() - age_minutes * 60
    os.utime(path, (stamp, stamp))
    return path


class PushSpoolDwellCheck(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        self.base = Path(self._temp.name)
        self.claude_dir = self.base / ".claude"
        self.state = self.claude_dir / "state"
        self.state.mkdir(parents=True)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def run_check(self, env: dict[str, str] | None = None) -> Doctor:
        doctor = Doctor(Path.cwd(), self.claude_dir, "settings")
        # Extra lane roots default to /root and /home/*; tests never scan the host.
        merged = {"CCC_DOCTOR_PUSH_STATE_ROOTS": "", **(env or {})}
        with patch.dict("os.environ", merged, clear=True):
            doctor.check_push_spool_dwell()
        return doctor

    def row(self, doctor: Doctor):
        rows = [r for r in doctor.rows if r.item == ITEM]
        self.assertEqual(len(rows), 1, [r.status for r in doctor.rows])
        return rows[0]

    # --- quiet nodes -----------------------------------------------------------

    def test_no_spool_dir_is_not_applicable(self) -> None:
        row = self.row(self.run_check())
        self.assertEqual(row.klass, "정상")
        self.assertIn("no push spool dir", row.status)

    def test_fresh_records_and_archive_are_normal(self) -> None:
        """A consumer polls every few seconds: young records and sent/ are fine."""
        spool = self.state / "telegram-spool"
        record(spool, "a.json", 2)
        record(spool / "sent", "old.json", 60 * 24 * 6)
        row = self.row(self.run_check())
        self.assertEqual(row.klass, "정상", row.status)
        self.assertIn("stale=0", row.status)
        self.assertIn("threshold=30m", row.status)

    def test_zero_threshold_disables_the_row(self) -> None:
        record(self.state / "telegram-spool", "a.json", 600)
        row = self.row(self.run_check({"CCC_DOCTOR_PUSH_SPOOL_DWELL_MINUTES": "0"}))
        self.assertEqual(row.klass, "정상")
        self.assertIn("disabled", row.status)

    def test_temp_and_non_json_files_are_ignored(self) -> None:
        spool = self.state / "telegram-spool"
        record(spool, ".pending.json.tmp", 600)
        record(spool, "notes.txt", 600)
        row = self.row(self.run_check())
        self.assertEqual(row.klass, "정상", row.status)

    # --- the incident shape ------------------------------------------------------

    def test_orphaned_writer_default_dir_warns_and_is_named(self) -> None:
        """Writers' default dir is stale while the consumer drains matrix-spool."""
        orphan = self.state / "telegram-spool"
        for i in range(3):
            record(orphan, f"r{i}.json", 60 * 20 + i)
        record(self.state / "matrix-spool", "fresh.json", 1)
        row = self.row(self.run_check())
        self.assertEqual(row.klass, "경고", row.status)
        self.assertIn("stale=3 in 1 dir(s)", row.status)
        self.assertIn("telegram-spool(n=3,oldest=20h writer-default)", row.status)
        self.assertNotIn("matrix-spool", row.status)
        self.assertIn("ccc-node#2223", row.action)

    def test_threshold_is_configurable(self) -> None:
        record(self.state / "telegram-spool", "a.json", 10)
        self.assertEqual(self.row(self.run_check()).klass, "정상")
        row = self.row(self.run_check({"CCC_DOCTOR_PUSH_SPOOL_DWELL_MINUTES": "5"}))
        self.assertEqual(row.klass, "경고", row.status)
        self.assertIn("oldest=10m", row.status)

    def test_overridden_writer_spool_is_marked_default(self) -> None:
        custom = self.base / "custom-spool"
        record(custom, "a.json", 90)
        row = self.row(self.run_check({"CCC_PUSH_SPOOL": str(custom)}))
        self.assertEqual(row.klass, "경고", row.status)
        self.assertIn("custom-spool(n=1,oldest=2h writer-default)", row.status)

    def test_fanout_mirror_subdir_is_checked(self) -> None:
        """Mirrors live inside the primary spool; an undrained mirror is an orphan too."""
        mirror = self.state / "telegram-spool" / "fanout-telegram"
        record(mirror, "a.json", 120)
        row = self.row(self.run_check())
        self.assertEqual(row.klass, "경고", row.status)
        self.assertIn("fanout-telegram(n=1", row.status)

    def test_other_lane_state_root_is_checked(self) -> None:
        """A root-lane writer next to a user-lane bridge (#2223 variant B)."""
        other = self.base / "other-home" / ".claude" / "state"
        record(other / "telegram-spool", "a.json", 60 * 24 * 30)
        row = self.row(
            self.run_check({"CCC_DOCTOR_PUSH_STATE_ROOTS": str(self.base / "*" / ".claude" / "state")})
        )
        self.assertEqual(row.klass, "경고", row.status)
        self.assertIn(str(other / "telegram-spool"), row.status)
        self.assertNotIn("writer-default", row.status)

    def test_symlinked_orphan_counts_once(self) -> None:
        """After the remedy (orphan dir -> consumed dir) a record is reported once."""
        consumed = self.state / "matrix-spool"
        record(consumed, "a.json", 90)
        (self.state / "telegram-spool").symlink_to(consumed, target_is_directory=True)
        row = self.row(self.run_check())
        self.assertIn("stale=1 in 1 dir(s)", row.status)

    def test_several_dirs_list_oldest_first(self) -> None:
        record(self.state / "telegram-spool", "a.json", 60)
        record(self.state / "matrix-spool", "b.json", 600)
        row = self.row(self.run_check())
        self.assertIn("stale=2 in 2 dir(s)", row.status)
        self.assertLess(row.status.index("matrix-spool"), row.status.index("telegram-spool"))

    def test_unreadable_dir_is_reported_not_fatal(self) -> None:
        spool = self.state / "telegram-spool"
        record(spool, "a.json", 1)
        doctor = Doctor(Path.cwd(), self.claude_dir, "settings")
        with patch.dict("os.environ", {"CCC_DOCTOR_PUSH_STATE_ROOTS": ""}, clear=True), patch.object(
            Doctor, "_push_spool_stale", return_value=None
        ):
            doctor.check_push_spool_dwell()
        row = self.row(doctor)
        self.assertEqual(row.klass, "정상", row.status)
        self.assertIn("unreadable=1", row.status)


if __name__ == "__main__":
    unittest.main()
