"""Tests for telegram_bot.utils.orphan_reaper."""

from __future__ import annotations

import asyncio
import os
import signal
import unittest
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_stat(ppid: int, starttime_ticks: int) -> str:
    """Build a minimal /proc/<pid>/stat string for the given ppid/starttime.

    After rfind(")") + 2, _parse_stat splits on spaces:
        fields[0] = state, fields[1] = ppid, ..., fields[19] = starttime

    The format string is "pid (comm) S el0 el1 ... el19", so after stripping
    the state token S:
        fields[1] = el0 = fields_after_state[0]  → ppid
        fields[19] = el18 = fields_after_state[18] → starttime
    """
    # 20 elements after state: el0..el19
    fields_after_state = ["0"] * 20
    fields_after_state[0] = str(ppid)        # el0 → fields[1] in _parse_stat
    fields_after_state[18] = str(starttime_ticks)  # el18 → fields[19] in _parse_stat
    return f"42 (node) S {' '.join(fields_after_state)}"


def _make_cmdline(parts: list[str]) -> bytes:
    return b"\x00".join(p.encode() for p in parts) + b"\x00"


# ---------------------------------------------------------------------------
# Unit tests for _parse_stat
# ---------------------------------------------------------------------------

class TestParseStat(unittest.TestCase):
    def setUp(self):
        from telegram_bot.utils import orphan_reaper
        self.mod = orphan_reaper

    def test_normal_case(self):
        stat = _make_stat(ppid=1, starttime_ticks=5000)
        result = self.mod._parse_stat(stat)
        self.assertIsNotNone(result)
        ppid, starttime = result
        self.assertEqual(ppid, 1)
        self.assertEqual(starttime, 5000)

    def test_comm_with_spaces(self):
        # comm field may contain spaces — last ')' must be used.
        # After "42 (my (weird) proc) S", fields[0]=state, fields[1]=ppid, fields[19]=starttime.
        # In the elements list el0..el19: el0 → fields[1]=ppid, el18 → fields[19]=starttime.
        els = ["0"] * 20
        els[0] = "999"    # el0 → fields[1] = ppid
        els[18] = "12345" # el18 → fields[19] = starttime
        stat = f"42 (my (weird) proc) S {' '.join(els)}"
        result = self.mod._parse_stat(stat)
        self.assertIsNotNone(result)
        ppid, starttime = result
        self.assertEqual(ppid, 999)
        self.assertEqual(starttime, 12345)

    def test_no_close_paren(self):
        self.assertIsNone(self.mod._parse_stat("42 node S 0 1"))

    def test_too_few_fields(self):
        self.assertIsNone(self.mod._parse_stat("42 (node) S 0"))


# ---------------------------------------------------------------------------
# Unit tests for _is_node_claude
# ---------------------------------------------------------------------------

class TestIsNodeClaude(unittest.TestCase):
    def setUp(self):
        from telegram_bot.utils import orphan_reaper
        self.mod = orphan_reaper

    def _check(self, parts: list[str]) -> bool:
        cmdline = " ".join(parts)
        return self.mod._is_node_claude(cmdline)

    def test_matches_node_claude_path(self):
        self.assertTrue(self._check(["/usr/bin/node", "/home/user/.npm/claude"]))

    def test_matches_node_claude_flag(self):
        self.assertTrue(self._check(["node", "claude", "--output-format", "stream-json"]))

    def test_termux_path(self):
        self.assertTrue(self._check(
            ["/data/data/com.termux/files/usr/bin/node",
             "/data/data/com.termux/files/home/.npm-global/lib/node_modules/@anthropic-ai/claude-code/cli.js"]
        ))

    def test_rejects_bare_node(self):
        self.assertFalse(self._check(["node", "server.js"]))

    def test_rejects_non_node(self):
        self.assertFalse(self._check(["python3", "claude"]))

    def test_rejects_empty(self):
        self.assertFalse(self._check([]))

    def test_rejects_one_part(self):
        self.assertFalse(self._check(["node"]))

    def test_case_insensitive(self):
        self.assertTrue(self._check(["Node", "Claude"]))

    def test_argv_list_with_spaced_node_path(self):
        # Regression: a node binary under a path with a space must still be
        # detected. The old code joined argv with spaces then re-split, turning
        # "/opt/my node/bin/node" into two tokens and missing the orphan. The
        # argv-list form preserves the boundary.
        argv = ["/opt/my node/bin/node", "/home/x/.npm/claude", "--print"]
        self.assertTrue(self.mod._is_node_claude(argv))
        # The lossy space-joined form is exactly what used to fail.
        self.assertFalse(self.mod._is_node_claude(" ".join(argv)))


# ---------------------------------------------------------------------------
# Unit tests for _is_orphaned_claude_process
# ---------------------------------------------------------------------------

class TestIsOrphanedClaudeProcess(unittest.TestCase):
    def setUp(self):
        from telegram_bot.utils import orphan_reaper
        self.mod = orphan_reaper

    def _call(self, pid=42, ppid=1, age_secs=7200, min_age=1800,
              cmdline=None):
        hz = 100
        uptime = 100000.0
        starttime = int(uptime * hz) - int(age_secs * hz)
        stat_str = _make_stat(ppid=ppid, starttime_ticks=starttime)
        if cmdline is None:
            cmdline_bytes = _make_cmdline(["node", "claude", "--output-format", "stream-json"])
        else:
            cmdline_bytes = _make_cmdline(cmdline)

        def fake_read_text(path):
            if path == f"/proc/{pid}/stat":
                return stat_str
            return None

        def fake_read_bytes(path):
            if path == f"/proc/{pid}/cmdline":
                return cmdline_bytes
            return None

        with (
            patch.object(self.mod, "_read_text", side_effect=fake_read_text),
            patch.object(self.mod, "_read_bytes", side_effect=fake_read_bytes),
        ):
            return self.mod._is_orphaned_claude_process(pid, min_age, uptime, hz)

    def test_qualifies_as_orphan(self):
        self.assertTrue(self._call())

    def test_too_young(self):
        self.assertFalse(self._call(age_secs=60, min_age=1800))

    def test_ppid_not_one(self):
        self.assertFalse(self._call(ppid=5000))

    def test_not_node_claude(self):
        self.assertFalse(self._call(cmdline=["python3", "bot.py"]))

    def test_stat_unreadable(self):
        with (
            patch.object(self.mod, "_read_text", return_value=None),
            patch.object(self.mod, "_read_bytes", return_value=b"node\x00claude\x00"),
        ):
            result = self.mod._is_orphaned_claude_process(42, 1800, 100000.0, 100)
        self.assertFalse(result)


# ---------------------------------------------------------------------------
# Unit tests for find_orphaned_claude_pids
# ---------------------------------------------------------------------------

class TestFindOrphanedClaudePids(unittest.TestCase):
    def setUp(self):
        from telegram_bot.utils import orphan_reaper
        self.mod = orphan_reaper

    def test_finds_orphans(self):
        """Two eligible PIDs are returned; non-numeric entries and own-pid are skipped."""
        own_pid = os.getpid()
        pids_in_proc = ["1", "2", str(own_pid), "999", "non-numeric", "8888"]

        def fake_is_orphan(pid, *args, **kwargs):
            return pid in (999, 8888)

        with (
            patch("os.listdir", return_value=pids_in_proc),
            patch.object(self.mod, "_is_orphaned_claude_process", side_effect=fake_is_orphan),
            patch.object(self.mod, "_get_hz", return_value=100),
            patch.object(self.mod, "_get_uptime_seconds", return_value=100000.0),
        ):
            result = self.mod.find_orphaned_claude_pids(min_age_seconds=1800)

        self.assertEqual(result, [999, 8888])

    def test_empty_when_no_orphans(self):
        with (
            patch("os.listdir", return_value=["1", "2"]),
            patch.object(self.mod, "_is_orphaned_claude_process", return_value=False),
            patch.object(self.mod, "_get_hz", return_value=100),
            patch.object(self.mod, "_get_uptime_seconds", return_value=100000.0),
        ):
            result = self.mod.find_orphaned_claude_pids()
        self.assertEqual(result, [])

    def test_proc_listdir_error(self):
        with patch("os.listdir", side_effect=OSError("no /proc")):
            result = self.mod.find_orphaned_claude_pids()
        self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# Unit tests for sweep_orphaned_claude_processes
# ---------------------------------------------------------------------------

class TestSweepOrphanedClaudeProcesses(unittest.TestCase):
    def setUp(self):
        from telegram_bot.utils import orphan_reaper
        self.mod = orphan_reaper

    def test_kills_found_pids(self):
        with (
            patch.object(self.mod, "find_orphaned_claude_pids", return_value=[111, 222]),
            patch("os.kill") as mock_kill,
        ):
            result = self.mod.sweep_orphaned_claude_processes()

        self.assertEqual(result, [111, 222])
        mock_kill.assert_any_call(111, signal.SIGTERM)
        mock_kill.assert_any_call(222, signal.SIGTERM)

    def test_returns_empty_when_no_orphans(self):
        with (
            patch.object(self.mod, "find_orphaned_claude_pids", return_value=[]),
            patch("os.kill") as mock_kill,
        ):
            result = self.mod.sweep_orphaned_claude_processes()

        self.assertEqual(result, [])
        mock_kill.assert_not_called()

    def test_skips_already_dead_process(self):
        def kill_side_effect(pid, sig):
            if pid == 111:
                raise ProcessLookupError("no such process")

        with (
            patch.object(self.mod, "find_orphaned_claude_pids", return_value=[111, 222]),
            patch("os.kill", side_effect=kill_side_effect),
        ):
            result = self.mod.sweep_orphaned_claude_processes()

        # 111 raised ProcessLookupError → not included; 222 succeeded
        self.assertEqual(result, [222])

    def test_skips_permission_denied(self):
        with (
            patch.object(self.mod, "find_orphaned_claude_pids", return_value=[333]),
            patch("os.kill", side_effect=PermissionError("not root")),
        ):
            result = self.mod.sweep_orphaned_claude_processes()

        self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# Async test for run_periodic_reaper
# ---------------------------------------------------------------------------

class TestRunPeriodicReaper(unittest.IsolatedAsyncioTestCase):
    async def test_sweeps_on_interval(self):
        from telegram_bot.utils.orphan_reaper import run_periodic_reaper

        call_count = 0

        async def fake_sleep(secs):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                raise asyncio.CancelledError

        with (
            patch("asyncio.sleep", side_effect=fake_sleep),
            patch(
                "telegram_bot.utils.orphan_reaper.sweep_orphaned_claude_processes",
                return_value=[42],
            ) as mock_sweep,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await run_periodic_reaper(interval_seconds=900, min_age_seconds=1800)

        # sweep called once before the second sleep raised CancelledError
        mock_sweep.assert_called_once_with(1800)

    async def test_handles_sweep_exception_and_continues(self):
        """An exception inside a sweep must not kill the reaper task."""
        from telegram_bot.utils.orphan_reaper import run_periodic_reaper

        sleep_count = 0

        async def fake_sleep(secs):
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count >= 3:
                raise asyncio.CancelledError

        sweep_count = 0

        def raising_sweep(min_age):
            nonlocal sweep_count
            sweep_count += 1
            if sweep_count == 1:
                raise RuntimeError("transient /proc read error")
            return []

        with (
            patch("asyncio.sleep", side_effect=fake_sleep),
            patch(
                "telegram_bot.utils.orphan_reaper.sweep_orphaned_claude_processes",
                side_effect=raising_sweep,
            ),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await run_periodic_reaper(interval_seconds=900, min_age_seconds=1800)

        # Sweep was attempted twice despite the first one raising
        self.assertEqual(sweep_count, 2)


if __name__ == "__main__":
    unittest.main()
