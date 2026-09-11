#!/usr/bin/env python3
"""Hermetic verdict tests for doctor's skill-promotion revise-stall check.

A `revise` verdict normally dispatches a revision round back to the author
node. When that node is not an online broker worker anywhere, the dispatch is
skipped and the findings are left "visible for human follow-up" — a comment
nobody re-reads. Nothing ages that state, so a permanent condition (an author
node that hosts a broker but runs no worker) looks exactly like a transient one
(a worker that is briefly offline). Fourteen PRs sat this way for twelve days
with the only trace being one line per PR in a cron log on the publisher.

These tests pin what makes the report trustworthy: absence is not drift, the
verdict follows the OLDEST skip rather than the count, an unparsable stamp
never reads as "brand new", and a stall never escalates past 경고 (it is not
locally repairable, so it must not block `doctor --fix`).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import json
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ccc_doctor import Doctor, _iso_age_days  # noqa: E402

ITEM = "skill-promotion revise stall"


def ago(days: float) -> str:
    return (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def skip_row(pr: str, stamp: str) -> dict:
    return {
        "kind": "a2a-revise-comment",
        "marker": "revise-verdict:revise_author_offline",
        "pr": pr,
        "head_sha": "a" * 40,
        "ts": stamp,
    }


class ReviseStallCheck(unittest.TestCase):
    def run_check(self, rows: list[dict] | None, *, mode: int = 0o600) -> Doctor:
        """Run the check against a temp claude_dir. `rows` None => no ledger."""
        with TemporaryDirectory() as temp:
            claude_dir = Path(temp) / ".claude"
            state = claude_dir / "state"
            state.mkdir(parents=True)
            if rows is not None:
                promo = state / "skill-promotion"
                promo.mkdir(parents=True)
                ledger = promo / "ledger.jsonl"
                ledger.write_text(
                    "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
                ledger.chmod(mode)
            doctor = Doctor(Path.cwd(), claude_dir, "settings")
            with patch.dict("os.environ", {}, clear=True):
                doctor.check_skill_promotion_revise_stall()
            return doctor

    def assert_row(self, doctor: Doctor, klass: str, fragment: str) -> None:
        row = doctor.rows[-1]
        self.assertEqual(row.item, ITEM)
        self.assertEqual(row.klass, klass, f"status was: {row.status}")
        self.assertIn(fragment, row.status)

    # --- absence is not drift ----------------------------------------------

    def test_missing_ledger_is_normal(self) -> None:
        """Only the publisher has a ledger; ordinary nodes must not raise drift."""
        self.assert_row(self.run_check(None), "정상", "ledger=absent")

    def test_ledger_without_skips_is_normal(self) -> None:
        rows = [{"kind": "a2a-dispatch", "dispatched_task": "t", "ts": ago(30)}]
        self.assert_row(self.run_check(rows), "정상", "skipped=0")

    # --- verdict follows the OLDEST skip -----------------------------------

    def test_recent_skip_is_normal(self) -> None:
        """A worker that is briefly offline is not a stall."""
        self.assert_row(self.run_check([skip_row("1", ago(1))]), "정상", "oldest=1d")

    def test_old_skip_warns(self) -> None:
        self.assert_row(self.run_check([skip_row("1", ago(12))]), "경고", "oldest=12d")

    def test_many_recent_skips_stay_normal(self) -> None:
        """A large but moving set is healthy — count alone must not warn."""
        rows = [skip_row(str(n), ago(1)) for n in range(40)]
        self.assert_row(self.run_check(rows), "정상", "skipped_prs=40")

    def test_one_old_skip_warns_despite_many_recent(self) -> None:
        rows = [skip_row(str(n), ago(1)) for n in range(40)] + [skip_row("99", ago(20))]
        self.assert_row(self.run_check(rows), "경고", "oldest=20d")

    def test_boundary_is_the_documented_window(self) -> None:
        from ccc_doctor import Doctor as D
        self.assertEqual(D._REVISE_STALL_DAYS, 7)
        self.assert_row(self.run_check([skip_row("1", ago(6.5))]), "정상", "oldest=6d")
        self.assert_row(self.run_check([skip_row("1", ago(7.5))]), "경고", "oldest=7d")

    # --- counting ----------------------------------------------------------

    def test_distinct_prs_are_counted_once(self) -> None:
        """The same PR skipped on every run is one stalled PR, not many."""
        rows = [skip_row("42", ago(d)) for d in (10, 9, 8, 7, 6)]
        self.assert_row(self.run_check(rows), "경고", "skipped_prs=1")

    def test_other_skip_codes_are_ignored(self) -> None:
        """revise_canon_lane is a different, intentional route — not a stall."""
        rows = [{"kind": "a2a-revise-comment",
                 "marker": "revise-verdict:revise_canon_lane",
                 "pr": "7", "ts": ago(30)}]
        self.assert_row(self.run_check(rows), "정상", "skipped=0")

    def test_wrong_kind_with_matching_text_is_ignored(self) -> None:
        """The fast substring prefilter must not decide the verdict by itself."""
        rows = [{"kind": "a2a-receipt", "status": "revise_author_offline",
                 "pr": "7", "ts": ago(30)}]
        self.assert_row(self.run_check(rows), "정상", "skipped=0")

    # --- malformed input ---------------------------------------------------

    def test_unparsable_stamp_never_reads_as_new(self) -> None:
        """A bad stamp must say 'cannot tell', not silently suppress a warning."""
        rows = [skip_row("1", "not-a-timestamp")]
        self.assert_row(self.run_check(rows), "정상", "oldest=unparsable")

    def test_corrupt_lines_do_not_break_the_scan(self) -> None:
        with TemporaryDirectory() as temp:
            claude_dir = Path(temp) / ".claude"
            promo = claude_dir / "state" / "skill-promotion"
            promo.mkdir(parents=True)
            ledger = promo / "ledger.jsonl"
            ledger.write_text(
                "{not json\n"
                + json.dumps(skip_row("5", ago(15))) + "\n"
                + "\n",
                encoding="utf-8")
            ledger.chmod(0o600)
            doctor = Doctor(Path.cwd(), claude_dir, "settings")
            with patch.dict("os.environ", {}, clear=True):
                doctor.check_skill_promotion_revise_stall()
        self.assert_row(doctor, "경고", "oldest=15d")

    # --- escalation boundary -----------------------------------------------

    def test_stall_never_escalates_past_warning(self) -> None:
        """Not locally repairable — must not block `doctor --fix`."""
        doctor = self.run_check([skip_row("1", ago(365))])
        self.assertEqual(doctor.rows[-1].klass, "경고")


class IsoAgeDaysHelper(unittest.TestCase):
    def test_parses_utc_stamp(self) -> None:
        self.assertEqual(_iso_age_days(ago(3)), 3)

    def test_returns_none_not_zero_for_garbage(self) -> None:
        for bad in ("", "not-a-timestamp", "2026-09-10", "2026-09-10T00:00:00"):
            with self.subTest(bad=bad):
                self.assertIsNone(_iso_age_days(bad))

    def test_future_stamp_clamps_to_zero(self) -> None:
        future = (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertEqual(_iso_age_days(future), 0)


if __name__ == "__main__":
    unittest.main()
