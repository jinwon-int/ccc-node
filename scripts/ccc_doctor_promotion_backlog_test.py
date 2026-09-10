#!/usr/bin/env python3
"""Hermetic verdict tests for doctor's skill-promotion backlog check.

The check exists because promotion is split across two hosts: an ordinary node
only stages envelopes into `state/skill-promotion/outbox/`, while a separately
enabled publisher collects them over SSH. Staging keeps reporting `ok` even
when the publisher never picks the node up, so the node cannot observe its own
promotion failure. These tests pin the properties that make the pull-based
report trustworthy — absence is not drift, the verdict follows the OLDEST
envelope rather than the count, and a stale queue never escalates past 경고.
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

DAY = 86400


class SkillPromotionBacklogCheck(unittest.TestCase):
    def run_check(self, envelopes: dict[str, int] | None, *, make_outbox: bool = True) -> Doctor:
        """Run the check against a temp claude_dir.

        `envelopes` maps a filename to its age in days; None skips creating the
        outbox directory entirely.
        """
        with TemporaryDirectory() as temp:
            claude_dir = Path(temp) / ".claude"
            state = claude_dir / "state"
            state.mkdir(parents=True)
            if envelopes is not None or make_outbox:
                outbox = state / "skill-promotion" / "outbox"
                outbox.mkdir(parents=True)
                now = time.time()
                for name, age in (envelopes or {}).items():
                    path = outbox / name
                    path.write_text("{}\n", encoding="utf-8")
                    stamp = now - age * DAY
                    os.utime(path, (stamp, stamp))
            doctor = Doctor(Path.cwd(), claude_dir, "settings")
            with patch.dict("os.environ", {}, clear=True):
                doctor.check_skill_promotion_backlog()
            return doctor

    def assert_row(self, doctor: Doctor, klass: str, fragment: str) -> None:
        row = doctor.rows[-1]
        self.assertEqual(row.item, "skill-promotion backlog")
        self.assertEqual(row.klass, klass, f"status was: {row.status}")
        self.assertIn(fragment, row.status)

    # --- absence is not drift ------------------------------------------------

    def test_missing_outbox_is_normal(self) -> None:
        """Promotion is opt-in; nodes that never staged must not raise drift."""
        doctor = self.run_check(None, make_outbox=False)
        self.assert_row(doctor, "정상", "outbox=absent")

    def test_empty_outbox_is_normal(self) -> None:
        doctor = self.run_check({})
        self.assert_row(doctor, "정상", "pending=0")

    # --- the verdict follows the oldest envelope, not the count --------------

    def test_fresh_queue_is_normal(self) -> None:
        doctor = self.run_check({f"e{i}.json": 1 for i in range(40)})
        self.assert_row(doctor, "정상", "pending=40")

    def test_large_but_moving_queue_is_normal(self) -> None:
        """A big queue whose oldest entry is recent means pickup still works."""
        doctor = self.run_check({f"e{i}.json": 13 for i in range(100)})
        self.assert_row(doctor, "정상", "oldest=13d")

    def test_single_stale_envelope_warns(self) -> None:
        """One envelope past the threshold is enough: pickup stopped."""
        doctor = self.run_check({"only.json": 30})
        self.assert_row(doctor, "경고", "pending=1; oldest=30d")

    def test_oldest_wins_over_fresh_majority(self) -> None:
        envelopes = {f"fresh{i}.json": 0 for i in range(20)}
        envelopes["stale.json"] = 21
        doctor = self.run_check(envelopes)
        self.assert_row(doctor, "경고", "oldest=21d")

    def test_threshold_boundary_is_inclusive_at_stale_side(self) -> None:
        self.assert_row(self.run_check({"a.json": 13}), "정상", "oldest=13d")
        self.assert_row(self.run_check({"a.json": 14}), "경고", "oldest=14d")

    # --- only envelopes count ------------------------------------------------

    def test_non_json_entries_are_ignored(self) -> None:
        """Lock files and scratch entries must not be read as envelopes."""
        doctor = self.run_check({"note.txt": 40, "real.json": 2})
        self.assert_row(doctor, "정상", "pending=1")

    # --- remedy names the publisher-side causes ------------------------------

    def test_warning_remedy_points_at_the_publisher(self) -> None:
        doctor = self.run_check({"a.json": 30})
        action = doctor.rows[-1].action
        self.assertIn("collect-nodes", action)
        self.assertIn("max_prs_per_run", action)

    # --- exit-code contract --------------------------------------------------

    def test_stale_backlog_never_blocks_repair(self) -> None:
        """A publisher-side stall is not locally fixable; it must not be 수동필요."""
        doctor = self.run_check({"a.json": 60})
        self.assertEqual(doctor.counts["수동필요"], 0)
        self.assertEqual(doctor.counts["경고"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
