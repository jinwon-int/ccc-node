#!/usr/bin/env python3
"""Hermetic verdict tests for doctor's fleet-skills sync check.

The check reads the owner-only `state/fleet-skills/last-run.json` receipt that
`ccc-fleet-skills-sync.py apply` leaves on every run. It exists because the
apply's verdict used to live only as a JSON line in the cron log: three nodes
failed every daily apply for 17 days (`target_user_owned`) while doctor kept
reporting the cron line as 정상. These tests pin the properties that make the
receipt trustworthy — absence is not drift, one failure is transient, a streak
warns, a successful run with user-owned skips warns by name, and nothing here
ever escalates past 경고 (the node cannot repair either condition with --fix).
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import json
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ccc_doctor import Doctor  # noqa: E402


class FleetSkillsSyncCheck(unittest.TestCase):
    def run_check(self, receipt: object | None, *, raw: str | None = None) -> Doctor:
        """Run the check against a temp claude_dir.

        `receipt` is JSON-dumped into last-run.json; None writes no receipt.
        `raw` writes the given bytes verbatim instead (malformed receipts).
        """
        with TemporaryDirectory() as temp:
            claude_dir = Path(temp) / ".claude"
            state = claude_dir / "state" / "fleet-skills"
            state.mkdir(parents=True)
            path = state / "last-run.json"
            if raw is not None:
                path.write_text(raw, encoding="utf-8")
            elif receipt is not None:
                path.write_text(json.dumps(receipt) + "\n", encoding="utf-8")
            doctor = Doctor(Path.cwd(), claude_dir, "settings")
            with patch.dict("os.environ", {}, clear=True):
                doctor.check_fleet_skills_sync()
            return doctor

    def assert_row(self, doctor: Doctor, klass: str, fragment: str) -> None:
        row = doctor.rows[-1]
        self.assertEqual(row.item, "fleet-skills sync")
        self.assertEqual(row.klass, klass, f"status was: {row.status}")
        self.assertIn(fragment, row.status)

    @staticmethod
    def receipt(**overrides: object) -> dict[str, object]:
        base: dict[str, object] = {
            "schema_version": 1,
            "ts": "2026-09-22T05:00:00Z",
            "ok": True,
            "mode": "apply",
            "commit": "d3fe322b9486182550a3355a9a5d06038e8d25f3",
            "code": None,
            "changed": 0,
            "skipped_user_owned": [],
            "consecutive_failures": 0,
        }
        base.update(overrides)
        return base

    # --- absence is not drift ------------------------------------------------

    def test_missing_receipt_is_normal(self) -> None:
        """Sync is opt-in and older installs never wrote a receipt."""
        self.assert_row(self.run_check(None), "정상", "last-run=absent")

    # --- a clean apply --------------------------------------------------------

    def test_ok_without_skips_is_normal(self) -> None:
        doctor = self.run_check(self.receipt())
        self.assert_row(doctor, "정상", "ok; commit=d3fe322b9486; skipped_user_owned=0")

    # --- one failure is transient; a streak is the finding -------------------

    def test_single_failure_is_normal(self) -> None:
        doctor = self.run_check(self.receipt(ok=False, code="locked", consecutive_failures=1))
        self.assert_row(doctor, "정상", "failed; code=locked; streak=1")

    def test_streak_boundary_warns_at_three(self) -> None:
        self.assert_row(
            self.run_check(self.receipt(ok=False, code="target_repo_not_private", consecutive_failures=2)),
            "정상", "streak=2",
        )
        self.assert_row(
            self.run_check(self.receipt(ok=False, code="target_repo_not_private", consecutive_failures=3)),
            "경고", "streak=3",
        )

    def test_long_streak_names_the_code_in_the_remedy(self) -> None:
        doctor = self.run_check(self.receipt(ok=False, code="target_conflict", consecutive_failures=17))
        self.assert_row(doctor, "경고", "code=target_conflict; streak=17")
        self.assertIn("17 runs in a row", doctor.rows[-1].action)
        self.assertIn("target_conflict", doctor.rows[-1].action)
        self.assertIn("plan --ref", doctor.rows[-1].action)

    # --- user-owned skips on a successful run --------------------------------

    def test_skipped_user_owned_warns_by_name(self) -> None:
        doctor = self.run_check(self.receipt(changed=3, skipped_user_owned=["claude:foo", "codex:foo"]))
        self.assert_row(doctor, "경고", "skipped_user_owned=2")
        self.assertIn("claude:foo, codex:foo", doctor.rows[-1].action)

    def test_many_skips_are_truncated_in_the_remedy(self) -> None:
        names = [f"claude:s{i}" for i in range(8)]
        doctor = self.run_check(self.receipt(skipped_user_owned=names))
        self.assert_row(doctor, "경고", "skipped_user_owned=8")
        self.assertIn("claude:s4", doctor.rows[-1].action)
        self.assertNotIn("claude:s5", doctor.rows[-1].action)
        self.assertIn("…", doctor.rows[-1].action)

    # --- malformed receipts ---------------------------------------------------

    def test_unreadable_receipt_needs_a_human(self) -> None:
        doctor = self.run_check(None, raw="{not json")
        self.assert_row(doctor, "수동필요", "last-run=unreadable")

    def test_non_object_receipt_needs_a_human(self) -> None:
        doctor = self.run_check(["ok"])
        self.assert_row(doctor, "수동필요", "last-run=unreadable")

    def test_garbage_streak_is_treated_as_zero(self) -> None:
        doctor = self.run_check(self.receipt(ok=False, code="x", consecutive_failures="lots"))
        self.assert_row(doctor, "정상", "streak=0")

    # --- exit-code contract --------------------------------------------------

    def test_findings_never_block_repair(self) -> None:
        """Neither a streak nor a skip is locally --fix-able; never 수동필요."""
        for receipt in (
            self.receipt(ok=False, code="target_conflict", consecutive_failures=30),
            self.receipt(skipped_user_owned=["claude:foo"]),
        ):
            doctor = self.run_check(receipt)
            self.assertEqual(doctor.counts["수동필요"], 0)
            self.assertEqual(doctor.counts["경고"], 1)


if __name__ == "__main__":
    unittest.main()
