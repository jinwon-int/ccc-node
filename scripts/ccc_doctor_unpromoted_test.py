#!/usr/bin/env python3
"""Hermetic verdict tests for doctor's skill-promotion unpromoted check.

An `approve` verdict is where the automated pipeline ends: it projects the
signed receipt, posts the verdict comment, and stops. Rebuilding a sanitized
`approved/*` PR from main and closing the intake PR is hand work with no
tooling behind it (policies/REVIEW.md: no auto-close, no auto-merge), and
nothing ages an approved candidate nobody promoted. Field case: 34 approved
candidates sat unpromoted — the oldest for weeks — and the count surfaced only
because someone classified the open PRs by hand.

These tests pin what makes the report trustworthy: absence is not drift, the
verdict follows the OLDEST outstanding approval rather than the count, a later
CLOSED always outvotes an earlier OPEN for the same PR, an unparsable stamp
never reads as "brand new", and the check never escalates past 경고 — the fix
is a human promotion round, so it must not block `doctor --fix`.
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

from ccc_doctor import Doctor  # noqa: E402

ITEM = "skill-promotion unpromoted"


def ago(days: float) -> str:
    """A timestamp in the exact format the promoter's _utc_now writes."""
    return (
        datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


def state_row(pr: str, state: str, approved_at: str) -> dict:
    """An `a2a-intake-state` row exactly as _sweep_intake_states writes it.

    Pinned to the producer on purpose. The B2 substitute gate shipped dead for
    weeks because its fixtures invented a row shape production never emitted
    (#1767), so the suite stayed green while the feature could not fire. The
    schema pin in ccc_skill_promotion_intake_state_test.py keeps this fixture
    honest from the other side.
    """
    return {
        "ts": approved_at,
        "kind": "a2a-intake-state",
        "pr": pr,
        "state": state,
        "approved_at": approved_at,
    }


class UnpromotedCheck(unittest.TestCase):
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
                    "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
                )
                ledger.chmod(mode)
            doctor = Doctor(Path.cwd(), claude_dir, "settings")
            with patch.dict("os.environ", {}, clear=True):
                doctor.check_skill_promotion_unpromoted()
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

    def test_ledger_without_state_rows_is_normal(self) -> None:
        rows = [{"kind": "a2a-dispatch", "dispatched_task": "t", "ts": ago(30)}]
        self.assert_row(self.run_check(rows), "정상", "unpromoted=0")

    def test_all_terminal_is_normal(self) -> None:
        """Promoted and closed is the healthy end state, however old."""
        rows = [state_row("1", "CLOSED", ago(90)), state_row("2", "MERGED", ago(90))]
        self.assert_row(self.run_check(rows), "정상", "unpromoted=0")

    # --- verdict follows the OLDEST outstanding approval --------------------

    def test_recent_approval_is_normal(self) -> None:
        """A batch approved today is in flight, not stalled."""
        self.assert_row(self.run_check([state_row("1", "OPEN", ago(1))]), "정상", "oldest=1d")

    def test_old_approval_warns(self) -> None:
        self.assert_row(self.run_check([state_row("1", "OPEN", ago(12))]), "경고", "oldest=12d")

    def test_threshold_boundary_is_inclusive(self) -> None:
        """7d is the warn edge — one day under must stay 정상."""
        self.assert_row(self.run_check([state_row("1", "OPEN", ago(6))]), "정상", "oldest=6d")
        self.assert_row(self.run_check([state_row("1", "OPEN", ago(7))]), "경고", "oldest=7d")

    def test_many_recent_approvals_stay_normal(self) -> None:
        """A large but moving batch is healthy — count alone must not warn."""
        rows = [state_row(str(n), "OPEN", ago(1)) for n in range(34)]
        self.assert_row(self.run_check(rows), "정상", "unpromoted=34")

    def test_one_old_approval_warns_despite_many_recent(self) -> None:
        rows = [state_row(str(n), "OPEN", ago(1)) for n in range(34)]
        rows.append(state_row("99", "OPEN", ago(20)))
        self.assert_row(self.run_check(rows), "경고", "oldest=20d")

    # --- last row per PR wins ----------------------------------------------

    def test_later_closed_outvotes_earlier_open(self) -> None:
        """The PR is re-polled until terminal; a stale OPEN must not linger."""
        rows = [state_row("1", "OPEN", ago(30)), state_row("1", "CLOSED", ago(30))]
        self.assert_row(self.run_check(rows), "정상", "unpromoted=0")

    def test_reopened_pr_counts_again(self) -> None:
        """Order, not precedence by state: the last observation is the truth."""
        rows = [state_row("1", "CLOSED", ago(30)), state_row("1", "OPEN", ago(30))]
        self.assert_row(self.run_check(rows), "경고", "unpromoted=1")

    # --- malformed input never reads as healthy ----------------------------

    def test_unparsable_stamp_does_not_read_as_new(self) -> None:
        rows = [state_row("1", "OPEN", "not-a-timestamp")]
        self.assert_row(self.run_check(rows), "정상", "oldest=unparsable")

    def test_rows_missing_keys_are_skipped(self) -> None:
        rows = [
            {"kind": "a2a-intake-state", "pr": "1"},
            {"kind": "a2a-intake-state", "state": "OPEN", "approved_at": ago(30)},
            {"kind": "a2a-intake-state", "pr": "2", "state": "OPEN"},
        ]
        self.assert_row(self.run_check(rows), "정상", "unpromoted=0")

    def test_corrupt_json_line_does_not_abort(self) -> None:
        with TemporaryDirectory() as temp:
            claude_dir = Path(temp) / ".claude"
            promo = claude_dir / "state" / "skill-promotion"
            promo.mkdir(parents=True)
            (promo / "ledger.jsonl").write_text(
                "{not json\n" + json.dumps(state_row("1", "OPEN", ago(20))) + "\n",
                encoding="utf-8",
            )
            doctor = Doctor(Path.cwd(), claude_dir, "settings")
            with patch.dict("os.environ", {}, clear=True):
                doctor.check_skill_promotion_unpromoted()
        self.assert_row(doctor, "경고", "oldest=20d")

    def test_other_kinds_are_ignored(self) -> None:
        """A substring match on the ledger line must not pull in other kinds."""
        rows = [
            {"kind": "a2a-verdict", "verdict": "approve", "ts": ago(40),
             "note": "a2a-intake-state"},
            state_row("1", "OPEN", ago(1)),
        ]
        self.assert_row(self.run_check(rows), "정상", "unpromoted=1")

    # --- never escalates past 경고 -----------------------------------------

    def test_stall_never_blocks_fix(self) -> None:
        """Promotion is a human round; the doctor must not claim it can repair it."""
        doctor = self.run_check([state_row("1", "OPEN", ago(60))])
        self.assertEqual(doctor.rows[-1].klass, "경고")

    def test_unreadable_ledger_is_manual(self) -> None:
        doctor = self.run_check([state_row("1", "OPEN", ago(20))], mode=0o000)
        row = doctor.rows[-1]
        self.assertEqual(row.item, ITEM)
        self.assertIn(row.klass, {"수동필요", "경고"})


if __name__ == "__main__":
    unittest.main(verbosity=0)
