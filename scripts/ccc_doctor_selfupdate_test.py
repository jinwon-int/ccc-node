#!/usr/bin/env python3
"""Hermetic verdict tests for doctor's self-update stall check (#1328).

The check exists because #1060's Telegram alert is a one-shot event: a node that
stalls while nobody reads chat stays stalled and invisible. These tests pin the
two properties that make the pull-based report trustworthy — the verdict follows
the LAST terminal record (not any abort in history), and the consecutive count
describes only the current incident.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ccc_doctor import Doctor  # noqa: E402

ABORT = "2026-08-27T19:45:02Z abort reason={reason} repo=/root/ccc-node"
WRONG_BRANCH = (
    "2026-08-27T19:45:02Z abort reason=wrong-branch repo=/root/ccc-node "
    "branch=skills/persist-session-patches expected=main"
)
DONE = "2026-08-26T20:45:01Z done result=up-to-date sha=9e9b58d8"
AUDIT = (
    '{"ts":"2026-08-26T19:45:14Z","result":"ok","old":"89a19f83","new":"9e9b58d8",'
    '"changed":true,"setup_ok":true,"services":[]}'
)
# A real run emits many of these between terminal records; none may be mistaken
# for an outcome.
NOISE = [
    "2026-08-26T19:45:09Z bridge-config-preflight result=ok",
    "2026-08-26T19:45:09Z reapply skip reason=current installer=scripts/install-nunchi.sh",
    "2026-08-26T19:45:14Z service name=ccc-telegram-bridge.service ok=true scope=system",
]


class SelfUpdateStallCheck(unittest.TestCase):
    def run_check(self, lines: list[str] | None) -> Doctor:
        """Run the check against a temp claude_dir, optionally seeding the log."""
        with TemporaryDirectory() as temp:
            claude_dir = Path(temp) / ".claude"
            state = claude_dir / "state"
            state.mkdir(parents=True)
            if lines is not None:
                (state / "self-update.log").write_text(
                    "\n".join(lines) + "\n", encoding="utf-8"
                )
            doctor = Doctor(Path.cwd(), claude_dir, "settings")
            with patch.dict("os.environ", {}, clear=True):
                doctor.check_self_update_stall()
        return doctor

    def assert_row(self, doctor: Doctor, klass: str, fragment: str) -> None:
        row = doctor.rows[-1]
        self.assertEqual(row.item, "self-update")
        self.assertEqual(row.klass, klass, f"status was: {row.status}")
        self.assertIn(fragment, row.status)

    # --- absence is not drift ------------------------------------------------

    def test_missing_log_is_normal(self) -> None:
        """Not every node runs self-update; a missing log must not raise drift."""
        doctor = self.run_check(None)
        self.assert_row(doctor, "정상", "log=absent")

    def test_log_without_terminal_records_is_normal(self) -> None:
        doctor = self.run_check(NOISE)
        self.assert_row(doctor, "정상", "last=none-recorded")

    # --- terminal outcome wins ----------------------------------------------

    def test_plain_done_line_reads_ok(self) -> None:
        doctor = self.run_check([*NOISE, DONE])
        self.assert_row(doctor, "정상", "last=ok")

    def test_jsonl_audit_record_reads_ok(self) -> None:
        """The log interleaves JSONL audit records; they are terminal too."""
        doctor = self.run_check([*NOISE, AUDIT])
        self.assert_row(doctor, "정상", "last=ok")

    def test_repaired_node_reads_ok_once_a_tick_succeeds(self) -> None:
        """An abort in history must not pin the node to stalled forever."""
        doctor = self.run_check([ABORT.format(reason="wrong-branch"), DONE])
        self.assert_row(doctor, "정상", "last=ok")

    def test_audit_record_also_closes_an_older_abort_streak(self) -> None:
        doctor = self.run_check([ABORT.format(reason="wrong-branch"), AUDIT])
        self.assert_row(doctor, "정상", "last=ok")

    # --- stalls -------------------------------------------------------------

    def test_wrong_branch_requires_manual_action(self) -> None:
        doctor = self.run_check([DONE, WRONG_BRANCH])
        self.assert_row(doctor, "수동필요", "reason=wrong-branch")
        self.assertIn("worktree", doctor.rows[-1].action)

    def test_wrong_branch_line_with_branch_detail_still_parses(self) -> None:
        """The reason field must survive the trailing branch=/expected= detail."""
        doctor = self.run_check([WRONG_BRANCH])
        self.assert_row(doctor, "수동필요", "consecutive=1")

    def test_dirty_tree_and_no_repo_require_manual_action(self) -> None:
        for reason in ("dirty-tree", "no-repo"):
            with self.subTest(reason=reason):
                doctor = self.run_check([DONE, ABORT.format(reason=reason)])
                self.assert_row(doctor, "수동필요", f"reason={reason}")

    def test_fetch_failed_is_only_a_warning(self) -> None:
        """Transient: the next tick may well succeed, so it must not fail exit."""
        doctor = self.run_check([DONE, ABORT.format(reason="fetch-failed")])
        self.assert_row(doctor, "경고", "reason=fetch-failed")
        self.assertEqual(doctor.counts["수동필요"], 0)

    # --- streak counts the current incident only ----------------------------

    def test_consecutive_counts_repeated_aborts(self) -> None:
        doctor = self.run_check(
            [DONE, ABORT.format(reason="wrong-branch"), *NOISE, WRONG_BRANCH]
        )
        self.assert_row(doctor, "수동필요", "consecutive=2")

    def test_streak_stops_at_a_different_reason(self) -> None:
        """A different earlier reason is a separate incident, not this one."""
        doctor = self.run_check(
            [
                ABORT.format(reason="dirty-tree"),
                ABORT.format(reason="wrong-branch"),
                WRONG_BRANCH,
            ]
        )
        self.assert_row(doctor, "수동필요", "consecutive=2")

    def test_streak_stops_at_an_intervening_success(self) -> None:
        doctor = self.run_check(
            [ABORT.format(reason="wrong-branch"), DONE, WRONG_BRANCH]
        )
        self.assert_row(doctor, "수동필요", "consecutive=1")

    # --- exit-code contract --------------------------------------------------

    def test_manual_row_drives_a_failing_exit_code(self) -> None:
        doctor = self.run_check([WRONG_BRANCH])
        self.assertEqual(doctor.counts["수동필요"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
