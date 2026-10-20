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
import os
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ccc_doctor import A2A_HANDLER_FILES, Doctor  # noqa: E402

SCRIPTS = Path(__file__).resolve().parent

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


class A2AHandlerDriftCheck(unittest.TestCase):
    """Installed A2A handler copies vs the checkout (#2357).

    The installer copies the handlers out of the checkout and self-update
    never refreshed them, so the fleet ran 09-10 handlers for weeks. Fixtures
    only: the system destination is redirected with CCC_A2A_HANDLER_SYSTEM_DEST
    and HOME is a temp dir, so the real /usr/local/sbin is never read.
    """

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo = self.root / "repo"
        (self.repo / "scripts").mkdir(parents=True)
        for name in A2A_HANDLER_FILES:
            (self.repo / "scripts" / name).write_text(f"#!/bin/sh\n# {name} v2\n", encoding="utf-8")
        self.home = self.root / "home"
        self.home.mkdir()
        self.sbin = self.root / "sbin"
        self.sbin.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def install(self, dest: Path, names: tuple[str, ...] = A2A_HANDLER_FILES) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        for name in names:
            (dest / name).write_bytes((self.repo / "scripts" / name).read_bytes())

    def run_check(self) -> Doctor:
        doctor = Doctor(self.repo, self.root / ".claude", "settings")
        env = {"HOME": str(self.home), "CCC_A2A_HANDLER_SYSTEM_DEST": str(self.sbin)}
        with patch.dict("os.environ", env, clear=True):
            doctor.check_a2a_handler_drift()
        return doctor

    def rows(self, doctor: Doctor) -> list:
        return [row for row in doctor.rows if row.item == "a2a-handlers"]

    def test_tuple_matches_installer_list(self) -> None:
        """The doctor's file set must be the installer's --list output."""
        out = subprocess.run(
            ["bash", str(SCRIPTS / "install-a2a-review-handler.sh"), "--list"],
            capture_output=True, text=True, check=True,
        ).stdout.split()
        self.assertEqual(tuple(out), A2A_HANDLER_FILES)

    def test_not_installed_is_normal(self) -> None:
        rows = self.rows(self.run_check())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].klass, "정상")
        self.assertEqual(rows[0].status, "not-installed")

    def test_identical_copies_are_ok(self) -> None:
        self.install(self.sbin)
        rows = self.rows(self.run_check())
        self.assertEqual([r.klass for r in rows], ["정상"])
        self.assertIn(f"dest={self.sbin}", rows[0].status)
        self.assertIn("current=4/4", rows[0].status)

    def test_drift_warns_naming_file_and_reinstall_command(self) -> None:
        self.install(self.sbin)
        (self.sbin / "skills-intake-revise-handler.sh").write_text("#!/bin/sh\n# 09-10\n", encoding="utf-8")
        doctor = self.run_check()
        rows = self.rows(doctor)
        self.assertEqual([r.klass for r in rows], ["경고"])
        self.assertIn("drift=skills-intake-revise-handler.sh", rows[0].status)
        self.assertNotIn("a2a-intent-dispatcher.sh", rows[0].status)
        self.assertIn("install-a2a-review-handler.sh --dest " + str(self.sbin), rows[0].action)
        # A warning must not fail the doctor exit code.
        self.assertEqual(doctor.report_exit_code(), 0)

    def test_partial_install_reports_missing_files(self) -> None:
        self.install(self.sbin, ("a2a-intent-dispatcher.sh",))
        rows = self.rows(self.run_check())
        self.assertEqual([r.klass for r in rows], ["경고"])
        self.assertIn("missing=danso-review-agent.sh", rows[0].status)
        self.assertNotIn("drift=", rows[0].status)

    def test_termux_home_dest_is_checked(self) -> None:
        """~/a2a-broker-worker (Termux / non-root) follows HOME."""
        dest = self.home / "a2a-broker-worker"
        self.install(dest)
        (dest / "a2a-intent-dispatcher.sh").write_text("old\n", encoding="utf-8")
        rows = self.rows(self.run_check())
        self.assertEqual([r.klass for r in rows], ["경고"])
        self.assertIn(f"dest={dest}", rows[0].status)
        self.assertIn("drift=a2a-intent-dispatcher.sh", rows[0].status)

    def test_unwritable_drifted_dest_names_the_owning_account(self) -> None:
        """self-update skips a dest it cannot write; doctor must say who can fix it."""
        self.install(self.sbin)
        (self.sbin / "a2a-intent-dispatcher.sh").write_text("old\n", encoding="utf-8")
        real_access = os.access
        sbin = self.sbin

        def access(path, mode, *args, **kwargs):  # root ignores 0555, so fake it
            if Path(path) == sbin and mode == os.W_OK:
                return False
            return real_access(path, mode, *args, **kwargs)

        with patch("ccc_doctor.os.access", side_effect=access):
            rows = self.rows(self.run_check())
        self.assertEqual([r.klass for r in rows], ["경고"])
        self.assertIn("not-writable", rows[0].status)
        self.assertIn("account that owns", rows[0].action)
        self.assertIn("--dest " + str(self.sbin), rows[0].action)

    def test_writable_drifted_dest_has_no_owner_hint(self) -> None:
        self.install(self.sbin)
        (self.sbin / "a2a-intent-dispatcher.sh").write_text("old\n", encoding="utf-8")
        rows = self.rows(self.run_check())
        self.assertNotIn("not-writable", rows[0].status)
        self.assertNotIn("account that owns", rows[0].action)

    def test_each_installed_dest_gets_its_own_row(self) -> None:
        self.install(self.sbin)
        self.install(self.home / "a2a-broker-worker")
        rows = self.rows(self.run_check())
        self.assertEqual([r.klass for r in rows], ["정상", "정상"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
