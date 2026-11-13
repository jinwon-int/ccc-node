#!/usr/bin/env python3
"""Unit tests for tasker_stall_watchdog.

The motivating outage: two relay tasks overlapped, both HTTP requests failed,
the execute service stopped and was started again three seconds later, and
from then on 995 profile triggers fired over almost eleven hours without one
task run. The app process stayed alive the whole time. These tests pin the
detection shape (triggers with no task line, old enough, enough of them), the
restart order (diagnostics, force-stop, launch, readiness), the cooldown, the
UNVERIFIED path that must never restart anything, and that run-log or logcat
text never reaches the page.

Run standalone: python3 scripts/tasker_stall_watchdog_test.py
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tasker_stall_watchdog as tsw

NOW = 1_794_500_000.0  # fixed epoch for deterministic runs
BODY_MARKER = "SENSITIVE_BODY_MARKER 1,234원 승인"
PKG = "com.example.automation"


def log_line(ts: float, kind: str, status: str, rest: str = "ID1 Relay Task") -> str:
    stamp = time.strftime("%Y%m%d %H.%M.%S", time.localtime(ts))
    return f"{stamp} {kind} {status:<8} {rest}"


def healthy_run(ts: float) -> list[str]:
    """One trigger that is followed by a task run, as in normal operation."""
    return [
        log_line(ts, "P", "Instant", f"ID9 Intercept Event {BODY_MARKER}"),
        log_line(ts, "E", "Start", "ID0:0.0 TaskService"),
        log_line(ts, "T", "Running"),
        log_line(ts + 1, "A", "OK", "ID1.1 Relay Task.HTTP Request"),
        log_line(ts + 1, "T", "ExitOK"),
        log_line(ts + 1, "E", "Stop", "ID0:0.0 TaskService"),
    ]


def stall(first: float, count: int, step: float) -> list[str]:
    """``count`` triggers from ``first``, none followed by a task line."""
    lines = [log_line(first, "E", "Start", "ID0:0.0 TaskService")]
    for i in range(count):
        lines.append(log_line(first + i * step, "P", "Instant", f"ID9 Intercept {BODY_MARKER}"))
    return lines


class Harness:
    """A fake adb that serves a run log and records every shell command."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls = root / "calls"
        self.runlog = root / "runlog.txt"
        self.rc = root / "rc"
        self.state_dir = root / "state"
        self.bin = root / "adb"
        self.bin.write_text(
            "#!/usr/bin/env bash\n"
            f'ROOT="{root}"\n'
            'cmd="${4:-}"\n'
            'printf "%s\\n" "$cmd" >> "$ROOT/calls"\n'
            'rc="$(cat "$ROOT/rc" 2>/dev/null || echo 0)"\n'
            'if [ "$rc" != 0 ]; then exit "$rc"; fi\n'
            'case "$cmd" in\n'
            '  *"tail -n"*) cat "$ROOT/runlog.txt" ;;\n'
            '  "am force-stop"*) : > "$ROOT/stopped" ;;\n'
            '  monkey*) if [ -e "$ROOT/monkey_fail" ]; then exit 1; fi; : > "$ROOT/launched" ;;\n'
            '  "dumpsys activity services"*)\n'
            '    if [ -e "$ROOT/launched" ] && [ ! -e "$ROOT/never_ready" ]; then\n'
            '      echo "ServiceRecord{1 u0 pkg/.MonitorService}"\n'
            '    else\n'
            '      echo "ServiceRecord{2 u0 pkg/.NotificationListenerService}"\n'
            '    fi ;;\n'
            '  "dumpsys activity service "*)\n'
            '    if [ -e "$ROOT/dump_hang" ]; then exit 1; fi; echo "SERVICE DUMP" ;;\n'
            f'  logcat*) echo "{BODY_MARKER}" ;;\n'
            '  "dumpsys activity exit-info"*) echo "exit info" ;;\n'
            'esac\n',
            encoding="utf-8",
        )
        self.bin.chmod(0o755)
        self.set_lines([])

    def set_lines(self, lines: list[str], rc: int = 0) -> None:
        self.runlog.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
        self.rc.write_text(str(rc), encoding="utf-8")

    def flag(self, name: str) -> None:
        (self.root / name).write_text("", encoding="utf-8")

    def commands(self) -> list[str]:
        try:
            return self.calls.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []

    def mutations(self) -> list[str]:
        return [c for c in self.commands() if c.startswith(("am force-stop", "monkey"))]

    def state(self) -> dict:
        return json.loads((self.state_dir / "state.json").read_text(encoding="utf-8"))

    def run(self, now: float = NOW, *extra: str) -> tuple[int, str]:
        argv = ["--state-dir", str(self.state_dir), "--package", PKG, "--now", str(now), *extra]
        out = io.StringIO()
        env = {"CCC_STALL_WATCHDOG_ADB": str(self.bin)}
        with mock.patch.dict(os.environ, env), mock.patch.object(tsw, "_sleep", lambda _s: None):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = tsw.main(argv)
        return code, out.getvalue()


class WatchdogTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.h = Harness(Path(self._tmp.name))

    # --- detection -------------------------------------------------------

    def test_healthy_log_is_ok_and_touches_nothing(self) -> None:
        self.h.set_lines(healthy_run(NOW - 900) + healthy_run(NOW - 600) + healthy_run(NOW - 60))
        code, out = self.h.run()
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("OK "))
        self.assertEqual(self.h.mutations(), [])
        self.assertEqual(self.h.state()["state"], "ok")

    def test_stall_saves_diagnostics_then_restarts(self) -> None:
        self.h.set_lines(healthy_run(NOW - 1800) + stall(NOW - 1200, 6, 200))
        code, out = self.h.run()
        self.assertEqual(code, 1)
        self.assertTrue(out.startswith("RESTARTED "), out)
        self.assertIn("6회", out)
        calls = self.h.commands()
        stop = next(i for i, c in enumerate(calls) if c.startswith("am force-stop"))
        launch = next(i for i, c in enumerate(calls) if c.startswith("monkey"))
        logcat = next(i for i, c in enumerate(calls) if c.startswith("logcat"))
        self.assertLess(logcat, stop, "diagnostics must be captured before the app is killed")
        self.assertLess(stop, launch)
        self.assertIn(PKG, calls[stop])
        state = self.h.state()
        self.assertEqual(state["state"], "restarted")
        self.assertEqual(state["lastRestartAt"], NOW)
        self.assertEqual(state["restarts"], 1)

    def test_recent_triggers_are_not_a_stall(self) -> None:
        self.h.set_lines(healthy_run(NOW - 900) + stall(NOW - 300, 5, 60))
        code, out = self.h.run()
        self.assertEqual(code, 0)
        self.assertIn("5회", out)
        self.assertEqual(self.h.mutations(), [])

    def test_too_few_triggers_are_not_a_stall(self) -> None:
        self.h.set_lines(healthy_run(NOW - 3600) + stall(NOW - 1800, 2, 600))
        code, _ = self.h.run()
        self.assertEqual(code, 0)
        self.assertEqual(self.h.mutations(), [])

    def test_task_line_clears_earlier_triggers(self) -> None:
        self.h.set_lines(stall(NOW - 3600, 8, 120) + healthy_run(NOW - 120))
        code, out = self.h.run()
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("OK "))
        self.assertEqual(self.h.mutations(), [])

    def test_rejected_copy_counts_as_task_activity(self) -> None:
        lines = stall(NOW - 3600, 8, 120) + [log_line(NOW - 60, "T", "RejCopy")]
        self.h.set_lines(lines)
        code, _ = self.h.run()
        self.assertEqual(code, 0)
        self.assertEqual(self.h.mutations(), [])

    def test_monitor_start_clears_earlier_triggers(self) -> None:
        lines = stall(NOW - 3600, 8, 120) + [log_line(NOW - 30, "M", "Start", "ID0:0.0 Monitor")]
        self.h.set_lines(lines)
        code, _ = self.h.run()
        self.assertEqual(code, 0)
        self.assertEqual(self.h.mutations(), [])

    def test_now_in_the_past_replays_that_moment(self) -> None:
        # At NOW the queue is moving again; twenty minutes earlier it was stalled.
        self.h.set_lines(stall(NOW - 3600, 10, 120) + healthy_run(NOW - 600))
        code, out = self.h.run(NOW - 1200, "--dry-run")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("DRY-RUN STALLED "), out)
        code, out = self.h.run(NOW, "--dry-run")
        self.assertTrue(out.startswith("OK "), out)

    def test_read_command_covers_the_rotated_file(self) -> None:
        self.h.set_lines(healthy_run(NOW - 60))
        self.h.run(NOW, "--runlog", "/sdcard/App/log/run.txt")
        read = self.h.commands()[0]
        self.assertIn("/sdcard/App/log/run.txt.1 /sdcard/App/log/run.txt", read)

    # --- restart guard rails ---------------------------------------------

    def test_dry_run_reports_without_side_effects(self) -> None:
        self.h.set_lines(stall(NOW - 1200, 6, 200))
        code, out = self.h.run(NOW, "--dry-run")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("DRY-RUN STALLED "))
        self.assertEqual(len(self.h.commands()), 1, "only the run-log read is allowed")
        self.assertFalse(self.h.state_dir.exists())

    def test_cooldown_blocks_a_second_restart_and_pages_once(self) -> None:
        self.h.set_lines(stall(NOW - 1200, 6, 200))
        self.assertEqual(self.h.run()[0], 1)
        later = NOW + 600
        self.h.set_lines(stall(NOW - 1200, 9, 200))
        code, out = self.h.run(later)
        self.assertEqual(code, 1)
        self.assertTrue(out.startswith("STALLED "), out)
        self.assertEqual(len([c for c in self.h.mutations() if c.startswith("am force-stop")]), 1)
        code, _ = self.h.run(later + 300)
        self.assertEqual(code, 0, "the cooldown page is sent once")
        self.assertEqual(len([c for c in self.h.mutations() if c.startswith("am force-stop")]), 1)

    def test_restart_is_allowed_again_after_the_cooldown(self) -> None:
        self.h.set_lines(stall(NOW - 1200, 6, 200))
        self.h.run()
        self.h.set_lines(stall(NOW - 1200, 20, 200))
        code, out = self.h.run(NOW + 31 * 60)
        self.assertEqual(code, 1)
        self.assertTrue(out.startswith("RESTARTED "), out)
        self.assertEqual(self.h.state()["restarts"], 2)

    def test_restart_without_ready_service_is_reported(self) -> None:
        self.h.flag("never_ready")
        self.h.set_lines(stall(NOW - 1200, 6, 200))
        code, out = self.h.run()
        self.assertEqual(code, 1)
        self.assertTrue(out.startswith("RESTART_FAILED "), out)
        self.assertEqual(self.h.state()["state"], "failed")
        # The failed attempt still starts the cooldown: no force-stop loop.
        code, out = self.h.run(NOW + 300)
        self.assertTrue(out.startswith("STALLED "), out)
        self.assertEqual(len([c for c in self.h.mutations() if c.startswith("am force-stop")]), 1)

    def test_launch_command_failure_is_reported(self) -> None:
        self.h.flag("monkey_fail")
        self.h.set_lines(stall(NOW - 1200, 6, 200))
        code, out = self.h.run()
        self.assertEqual(code, 1)
        self.assertTrue(out.startswith("RESTART_FAILED "), out)

    def test_held_lock_skips_the_run(self) -> None:
        self.h.set_lines(stall(NOW - 1200, 6, 200))
        self.h.state_dir.mkdir(parents=True)
        (self.h.state_dir / "lock").write_text("1\n", encoding="utf-8")
        code, out = self.h.run()
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("SKIP "))
        self.assertEqual(self.h.commands(), [])

    def test_stale_lock_is_replaced(self) -> None:
        self.h.set_lines(healthy_run(NOW - 60))
        self.h.state_dir.mkdir(parents=True)
        lock = self.h.state_dir / "lock"
        lock.write_text("1\n", encoding="utf-8")
        old = time.time() - tsw.LOCK_STALE_SEC - 60
        os.utime(lock, (old, old))
        code, out = self.h.run()
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("OK "))
        self.assertFalse(lock.exists())

    # --- unverified ------------------------------------------------------

    def test_adb_failure_is_unverified_and_never_restarts(self) -> None:
        self.h.set_lines(stall(NOW - 1200, 6, 200), rc=1)
        code, out = self.h.run()
        self.assertEqual(code, 1)
        self.assertTrue(out.startswith("NEW UNVERIFIED "), out)
        self.assertEqual(self.h.mutations(), [])
        code, _ = self.h.run(NOW + 300)
        self.assertEqual(code, 0, "no page on every run")
        code, out = self.h.run(NOW + 181 * 60)
        self.assertEqual(code, 1)
        self.assertTrue(out.startswith("STILL UNVERIFIED "), out)
        self.assertEqual(self.h.mutations(), [])

    def test_empty_runlog_is_unverified(self) -> None:
        self.h.set_lines([])
        code, out = self.h.run()
        self.assertEqual(code, 1)
        self.assertIn("UNVERIFIED", out)
        self.assertEqual(self.h.mutations(), [])

    def test_recovery_from_unverified_pages_once(self) -> None:
        self.h.set_lines([], rc=1)
        self.h.run()
        self.h.set_lines(healthy_run(NOW + 200))
        code, out = self.h.run(NOW + 300)
        self.assertEqual(code, 1)
        self.assertTrue(out.startswith("RECOVERED "), out)
        self.assertEqual(self.h.run(NOW + 600)[0], 0)

    # --- privacy and housekeeping ----------------------------------------

    def test_log_text_stays_in_private_diagnostics(self) -> None:
        self.h.set_lines(stall(NOW - 1200, 6, 200))
        code, out = self.h.run()
        self.assertEqual(code, 1)
        self.assertNotIn("SENSITIVE_BODY_MARKER", out)
        diag_root = self.h.state_dir / "diag"
        runs = [p for p in diag_root.iterdir() if p.is_dir()]
        self.assertEqual(len(runs), 1)
        self.assertEqual(stat.S_IMODE(runs[0].stat().st_mode), 0o700)
        names = sorted(p.name for p in runs[0].iterdir())
        self.assertEqual(names, ["exit-info.txt", "logcat.txt", "runlog-tail.txt",
                                 "service-dump.txt", "services.txt"])
        for path in runs[0].iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, path.name)
        self.assertIn("SENSITIVE_BODY_MARKER", (runs[0] / "logcat.txt").read_text("utf-8"))
        self.assertEqual(stat.S_IMODE((self.h.state_dir / "state.json").stat().st_mode), 0o600)

    def test_hung_service_dump_is_recorded_not_fatal(self) -> None:
        self.h.flag("dump_hang")
        self.h.set_lines(stall(NOW - 1200, 6, 200))
        code, out = self.h.run()
        self.assertTrue(out.startswith("RESTARTED "), out)
        run_dir = next(p for p in (self.h.state_dir / "diag").iterdir())
        self.assertIn("capture failed", (run_dir / "service-dump.txt").read_text("utf-8"))

    def test_old_diagnostics_are_pruned(self) -> None:
        for i in range(4):
            when = NOW + i * 3600
            self.h.set_lines(stall(when - 1200, 6, 200))
            (self.h.root / "launched").unlink(missing_ok=True)
            self.assertEqual(self.h.run(when, "--keep-diag", "2")[0], 1)
        kept = sorted(p.name for p in (self.h.state_dir / "diag").iterdir())
        self.assertEqual(len(kept), 2)
        newest = time.strftime("%Y%m%dT%H%M%S", time.localtime(NOW + 3 * 3600))
        self.assertEqual(kept[-1], newest)

    def test_usage_errors_exit_2(self) -> None:
        self.assertEqual(self.h.run(NOW, "--min-triggers", "0")[0], 2)
        self.assertEqual(self.h.run(NOW, "--package", "bad;rm -rf x")[0], 2)
        self.assertEqual(self.h.run(NOW, "--runlog", "/sdcard/$(id)")[0], 2)
        self.assertEqual(tsw.main([]), 2)
        self.assertEqual(self.h.commands(), [])


if __name__ == "__main__":
    unittest.main()
