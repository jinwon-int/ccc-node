#!/usr/bin/env python3
"""Hermetic verdict tests for doctor's nonharness-account-hooks check (#2323).

setup.sh and self-update only refresh the account whose home the live bridge
serves. A node's retired root lane kept a settings.json with 17 hooks from
09-11 and no update path; once a root Claude login was issued, root sessions
fired them. These tests pin that the doctor flags hook wiring left in any
other account, skips the harness account, reports counts and event names only
(never command text), and never edits the files it reads.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import hashlib
import inspect
import json
import os
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ccc_doctor import Doctor, describe_hook_events, hook_wiring_counts  # noqa: E402

ITEM = "nonharness-account-hooks"
REPO = Path(__file__).resolve().parent.parent
SECRET_COMMAND = "/opt/fixture/hooks/very-private-hook.sh --token-shaped-arg"


def hooks_doc(**events: int) -> dict:
    """A settings document wiring ``n`` hook commands under each event."""
    return {
        "outputStyle": "fixture",
        "hooks": {
            event: [{"matcher": "", "hooks": [{"type": "command", "command": SECRET_COMMAND}] * n}]
            for event, n in events.items()
        },
    }


class HookWiringCounts(unittest.TestCase):
    def test_counts_hook_entries_per_event(self) -> None:
        self.assertEqual(hook_wiring_counts(hooks_doc(SessionStart=2, PreToolUse=1)), {"SessionStart": 2, "PreToolUse": 1})

    def test_malformed_and_empty_shapes_are_not_wiring(self) -> None:
        for data in (
            None,
            [],
            {},
            {"hooks": []},
            {"hooks": {"SessionStart": "x"}},
            {"hooks": {"SessionStart": []}},
            {"hooks": {"SessionStart": [{"matcher": ""}]}},
            {"hooks": {"SessionStart": [{"hooks": []}]}},
            {"hooks": {"SessionStart": [{"hooks": ["not-a-dict"]}]}},
            {"disableAllHooks": True, **hooks_doc(SessionStart=3)},
        ):
            with self.subTest(data=data):
                self.assertEqual(hook_wiring_counts(data), {})


class DescribeHookEvents(unittest.TestCase):
    def test_forged_names_are_counted_not_printed(self) -> None:
        forged = "Stop\n정상 | bridge status | FAKE ROW\x1b[2J sk-ant-SECRET"
        text = describe_hook_events({"SessionStart": 1, forged: 1, "x" * 41: 1})
        self.assertEqual(text, "SessionStart,other=2")

    def test_shown_names_are_capped(self) -> None:
        counts = {f"Event{chr(65 + i)}": 1 for i in range(10)}
        text = describe_hook_events(counts)
        self.assertEqual(text.count(","), 8)
        self.assertTrue(text.endswith("other=2"))


class NonharnessAccountHooks(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.harness = self.root / "harness"
        self.other = self.root / "other"
        self.third = self.root / "third"
        for home in (self.harness, self.other, self.third):
            home.mkdir()
        self.claude_dir = self.harness / ".claude"
        self.claude_dir.mkdir()
        self.env = patch.dict(
            os.environ,
            {"CCC_DOCTOR_ACCOUNT_HOMES": os.pathsep.join(str(h) for h in (self.harness, self.other, self.third))},
        )
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        for path in self.root.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o755)
        self._tmp.cleanup()

    def write(self, home: Path, name: str, data: object) -> Path:
        claude = home / ".claude"
        claude.mkdir(exist_ok=True)
        path = claude / name
        path.write_text(json.dumps(data) if not isinstance(data, str) else data, encoding="utf-8")
        return path

    def run_check(
        self, bridge_lines: list[str] | None = None, owner_homes: list[str] | None = None
    ) -> tuple[str, str, str]:
        if bridge_lines is None:
            bridge_lines = [f"/opt/ccc-node/bridge/venv/bin/python -m telegram_bot --path {self.harness}"]
        doctor = Doctor(REPO, self.claude_dir, "settings")
        with patch.object(Doctor, "bridge_command_lines", return_value=bridge_lines), patch.object(
            Doctor, "bridge_owner_homes", return_value=owner_homes or []
        ):
            doctor.check_nonharness_account_hooks()
        rows = [row for row in doctor.rows if row.item == ITEM]
        self.assertEqual(len(rows), 1)
        return rows[0].klass, rows[0].status, rows[0].action

    def test_no_running_bridge_is_not_applicable(self) -> None:
        self.write(self.other, "settings.json", hooks_doc(SessionStart=3))
        klass, status, _ = self.run_check(bridge_lines=[])
        self.assertEqual(klass, "정상")
        self.assertIn("해당 없음", status)

    def test_harness_account_hooks_are_expected(self) -> None:
        self.write(self.harness, "settings.json", hooks_doc(SessionStart=5, PreToolUse=4))
        self.write(self.other, "settings.json", {"outputStyle": "fixture"})
        klass, status, _ = self.run_check()
        self.assertEqual(klass, "정상")
        self.assertIn(f"harness={self.harness}", status)
        self.assertIn("other accounts with ~/.claude=1", status)
        self.assertIn("leftover hooks=0", status)

    def test_retired_lane_with_hooks_warns_with_counts_only(self) -> None:
        settings = self.write(self.other, "settings.json", hooks_doc(SessionStart=2, PreToolUse=1))
        local = self.write(self.third, "settings.local.json", hooks_doc(SessionStart=1))
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (settings, local)}
        klass, status, action = self.run_check()
        self.assertEqual(klass, "경고")
        self.assertIn(f"{settings}: hooks=3 (PreToolUse,SessionStart)", status)
        self.assertIn(f"{local}: hooks=1 (SessionStart)", status)
        self.assertNotIn(SECRET_COMMAND, status + action)
        self.assertNotIn("very-private-hook", status + action)
        self.assertIn("#2323", action)
        after = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (settings, local)}
        self.assertEqual(before, after, "doctor must never edit another account's settings")

    def test_unlinked_hooks_key_is_quiet(self) -> None:
        """The 10-15 remedy (hooks key removed, login kept) reads as clean."""
        self.write(self.other, "settings.json", {"outputStyle": "fixture", "hooks": {}})
        klass, status, _ = self.run_check()
        self.assertEqual(klass, "정상")
        self.assertIn("leftover hooks=0", status)

    def test_bridge_path_inside_home_marks_that_home_as_harness(self) -> None:
        self.write(self.other, "settings.json", hooks_doc(SessionStart=1))
        work = self.other / "work"
        work.mkdir()
        klass, _, _ = self.run_check(
            bridge_lines=[f"/x/bridge/start.sh --path {self.harness}", f"/x/bridge/start.sh --path {work}"]
        )
        self.assertEqual(klass, "정상")

    def test_root_run_sees_its_own_hooks_when_it_is_not_the_harness(self) -> None:
        """A root run on a node whose harness is a user account must see root's own hooks."""
        self.write(self.other, "settings.json", hooks_doc(Stop=1))
        doctor = Doctor(REPO, self.other / ".claude", "settings")
        lines = [f"/x/bridge/start.sh --path {self.harness}"]
        with patch.object(Doctor, "bridge_command_lines", return_value=lines), patch.object(
            Doctor, "bridge_owner_homes", return_value=[]
        ):
            doctor.check_nonharness_account_hooks()
        row = next(row for row in doctor.rows if row.item == ITEM)
        self.assertEqual(row.klass, "경고")
        self.assertIn("hooks=1 (Stop)", row.status)

    def test_default_scan_includes_own_login_home_not_claude_dir_parent(self) -> None:
        os.environ.pop("CCC_DOCTOR_ACCOUNT_HOMES")
        target = self.root / "srv" / "state" / ".claude"
        target.mkdir(parents=True)
        doctor = Doctor(REPO, target, "settings")
        with patch.object(Doctor, "own_home", return_value=self.third):
            homes = doctor._account_homes()
        self.assertIn(self.third, homes)
        self.assertNotIn(target.parent, homes)

    def test_override_is_the_complete_account_list(self) -> None:
        doctor = Doctor(REPO, self.root / "elsewhere" / ".claude", "settings")
        with patch.object(Doctor, "own_home", return_value=self.root / "elsewhere"):
            homes = doctor._account_homes()
        self.assertEqual(homes, [self.harness, self.other, self.third])

    def test_home_whose_claude_is_the_harness_claude_is_skipped(self) -> None:
        """A symlinked ~/.claude that resolves to the harness's own dir is not foreign."""
        self.write(self.harness, "settings.json", hooks_doc(SessionStart=2))
        (self.other / ".claude").symlink_to(self.claude_dir)
        klass, status, _ = self.run_check()
        self.assertEqual(klass, "정상")
        self.assertIn("other accounts with ~/.claude=0", status)

    def test_bridge_owner_home_counts_as_harness(self) -> None:
        """CCC_BRIDGE_DEFAULT_PATH may serve a non-home dir; the owner's hooks are still the harness's."""
        self.write(self.other, "settings.json", hooks_doc(SessionStart=1))
        work = self.root / "work"
        work.mkdir()
        klass, status, _ = self.run_check(
            bridge_lines=[f"/x/bridge/start.sh --path {work}"], owner_homes=[str(self.other)]
        )
        self.assertEqual(klass, "정상")
        self.assertIn(str(self.other), status.split(";")[0])

    def test_deeply_nested_json_does_not_abort_the_doctor(self) -> None:
        bad = self.write(self.other, "settings.json", "[" * 100_000)
        klass, status, _ = self.run_check()
        self.assertEqual(klass, "정상")
        self.assertIn(f"{bad}(unparseable)", status)

    def test_forged_event_names_never_reach_the_row(self) -> None:
        forged = "Stop\n| 정상 | bridge status | FAKE ROW\x1b[2J"
        path = self.write(self.other, "settings.json", {"hooks": {forged: [{"hooks": [{"command": "x"}]}]}})
        klass, status, _ = self.run_check()
        self.assertEqual(klass, "경고")
        self.assertIn(f"{path}: hooks=1 (other=1)", status)
        self.assertNotIn("FAKE ROW", status)
        self.assertNotIn("\n", status)

    def test_disable_all_hooks_reads_as_clean(self) -> None:
        self.write(self.other, "settings.json", {"disableAllHooks": True, **hooks_doc(SessionStart=4)})
        klass, _, _ = self.run_check()
        self.assertEqual(klass, "정상")

    def test_account_without_claude_dir_is_not_counted(self) -> None:
        (self.third / ".claude").write_text("not a dir", encoding="utf-8")
        klass, status, _ = self.run_check()
        self.assertEqual(klass, "정상")
        self.assertIn("other accounts with ~/.claude=0", status)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root reads through mode 000")
    def test_unreadable_account_is_listed_not_flagged(self) -> None:
        self.write(self.other, "settings.json", hooks_doc(SessionStart=1))
        self.other.chmod(0o000)
        klass, status, _ = self.run_check()
        self.assertEqual(klass, "정상")
        self.assertIn(f"unreadable={self.other}", status)

    def test_symlinked_settings_is_not_followed_but_listed(self) -> None:
        target = self.write(self.third, "settings.json", hooks_doc(SessionStart=1))
        (self.other / ".claude").mkdir()
        link = self.other / ".claude" / "settings.json"
        link.symlink_to(target)
        os.environ["CCC_DOCTOR_ACCOUNT_HOMES"] = os.pathsep.join(str(h) for h in (self.harness, self.other))
        klass, status, _ = self.run_check()
        self.assertEqual(klass, "정상")
        self.assertIn("other accounts with ~/.claude=1", status)
        self.assertIn(f"{link}(symlink, not followed)", status)

    def test_fifo_settings_does_not_block(self) -> None:
        if not hasattr(os, "mkfifo"):
            self.skipTest("no mkfifo")
        (self.other / ".claude").mkdir()
        os.mkfifo(self.other / ".claude" / "settings.json")
        klass, _, _ = self.run_check()
        self.assertEqual(klass, "정상")

    def test_oversize_and_unparseable_settings_are_unreadable(self) -> None:
        big = self.write(self.other, "settings.json", " " * ((1 << 20) + 1))
        bad = self.write(self.third, "settings.json", "{not json")
        klass, status, _ = self.run_check()
        self.assertEqual(klass, "정상")
        self.assertIn(str(big), status)
        self.assertIn(f"{bad}(unparseable)", status)

    def test_diagnose_runs_the_check(self) -> None:
        self.assertIn("self.check_nonharness_account_hooks()", inspect.getsource(Doctor.diagnose))


if __name__ == "__main__":
    unittest.main()
