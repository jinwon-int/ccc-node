#!/usr/bin/env python3
"""Unit tests for memory_guard_observe (#2633 a).

The motivating observation: sogyo/hande reported the same ``1.9G memory
peak, 257.4M memory swap peak`` on four consecutive self-update stops
(2026-11-01) because each stop left unit processes behind, the next start
reused the cgroup, and the kernel counters never reset. These tests pin the
per-cycle peaks, the reused-cgroup flag, the events delta keyed by cgroup id,
the verdict ladder, and that journal message text never reaches the output.

Run standalone: python3 scripts/memory_guard_observe_test.py
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import memory_guard_observe as mgo

UNIT = "ccc-hande-bridge.service"
SECRET_COMM = "SENSITIVE_COMM_MARKER"
HIGH = 2463432704
MAX = 3284578304

# Shaped after the sogyo journal (short-unix), with a leftover-reusing restart.
SOGYO_JOURNAL = f"""\
1761916674.0 sogyo systemd[1]: Stopping {UNIT} - ccc-node hande frontend...
1761916678.0 sogyo systemd[1]: {UNIT}: Failed to kill control group /system.slice/{UNIT}, ignoring: Invalid argument
1761916678.0 sogyo systemd[1]: Stopped {UNIT} - ccc-node hande frontend.
1761916678.0 sogyo systemd[1]: {UNIT}: Consumed 1h 36min 11.312s CPU time, 1.9G memory peak, 257.4M memory swap peak.
1761916678.0 sogyo systemd[1]: Started {UNIT} - ccc-node hande frontend.
1761928000.0 sogyo systemd[1]: {UNIT}: Killing process 22478 ({SECRET_COMM}) with signal SIGKILL.
1761928000.0 sogyo systemd[1]: {UNIT}: Unit process 22364 ({SECRET_COMM}) remains running after unit stopped.
1761928000.0 sogyo systemd[1]: {UNIT}: Unit process 22445 ({SECRET_COMM}) remains running after unit stopped.
1761928000.0 sogyo systemd[1]: {UNIT}: Consumed 1min 45.083s CPU time, 730.1M memory peak, 0B memory swap peak.
1761928000.0 sogyo systemd[1]: {UNIT}: Found left-over process 22364 ({SECRET_COMM}) in control group while starting unit. Ignoring.
1761928000.0 sogyo systemd[1]: Started {UNIT} - ccc-node hande frontend.
1761929000.0 sogyo systemd[1]: {UNIT}: Consumed 3min 53.630s CPU time.
"""


class Harness:
    """Fake systemctl/journalctl binaries and a fake cgroup v2 tree."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.cg_root = root / "cgroup"
        self.cg = self.cg_root / "system.slice" / UNIT
        self.cg.mkdir(parents=True)
        self.show = root / "show.txt"
        self.journal = root / "journal.txt"
        self.show_rc = root / "show_rc"
        self.state = root / "state" / "memguard.json"
        systemctl = root / "systemctl"
        systemctl.write_text(
            "#!/bin/sh\n"
            f'case "$1" in\n'
            f'  list-units) printf "%s loaded active running x\\n" {UNIT}; exit 0;;\n'
            f'  show) [ -f "{self.show_rc}" ] && exit "$(cat "{self.show_rc}")"; cat "{self.show}";;\n'
            "esac\n"
        )
        journalctl = root / "journalctl"
        journalctl.write_text(f'#!/bin/sh\ncat "{self.journal}"\n')
        for exe in (systemctl, journalctl):
            exe.chmod(0o755)
        self.env = {
            "CCC_MEMGUARD_SYSTEMCTL": str(systemctl),
            "CCC_MEMGUARD_JOURNALCTL": str(journalctl),
            "CCC_MEMGUARD_CGROUP_ROOT": str(self.cg_root),
            "CCC_MEMGUARD_NOW": "1762000000",
        }
        self.set_show()
        self.journal.write_text("")
        self.set_cgroup()

    def set_show(self, active: str = "active", cg_id: str = "4242") -> None:
        self.show.write_text(
            f"ActiveState={active}\nMainPID=44328\nExecMainStartTimestamp=Mon 2026-11-02 19:17:39 KST\n"
            f"ControlGroup=/system.slice/{UNIT}\nControlGroupId={cg_id}\nInvocationID=abc\n"
        )

    def set_cgroup(self, *, peak: int = 849514496, high: int = 0, mx: int = 0, oom_kill: int = 0,
                   some300: float = 0.0) -> None:
        files = {
            "memory.high": f"{HIGH}\n", "memory.max": f"{MAX}\n",
            "memory.current": "637079552\n", "memory.peak": f"{peak}\n",
            "memory.swap.peak": "0\n",
            "memory.events": f"low 0\nhigh {high}\nmax {mx}\noom 0\noom_kill {oom_kill}\noom_group_kill 0\n",
            "memory.pressure": (
                f"some avg10=0.00 avg60=0.00 avg300={some300:.2f} total=0\n"
                "full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
            ),
        }
        for name, content in files.items():
            (self.cg / name).write_text(content)

    def run(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.env), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = mgo.main(list(args))
        return rc, out.getvalue(), err.getvalue()

    def run_json(self, *args: str) -> tuple[int, dict]:
        rc, out, _ = self.run("--json", "--unit", UNIT, *args)
        lines = [line for line in out.splitlines() if line.strip()]
        assert len(lines) == 1, out
        return rc, json.loads(lines[0])


class JournalParsing(unittest.TestCase):
    def test_cycles_peaks_survivors_and_reused_start(self) -> None:
        lines = [(float(ts), msg) for ts, msg in (row.split(" ", 1) for row in SOGYO_JOURNAL.splitlines())]
        parsed = mgo.parse_journal(lines)
        peaks = [c["memory_peak"] for c in parsed["cycles"]]
        self.assertEqual(peaks, [int(1.9 * (1 << 30)), int(730.1 * (1 << 20)), None])
        self.assertEqual(parsed["cycles"][0]["swap_peak"], int(257.4 * (1 << 20)))
        self.assertEqual(parsed["cycles"][0]["cpu"], "1h 36min 11.312s")
        self.assertEqual([c["survivors"] for c in parsed["cycles"]], [0, 2, 0])
        self.assertEqual([s["leftover"] for s in parsed["starts"]], [0, 1])
        self.assertEqual(parsed["journal_oom_kills"], 0)

    def test_size_units(self) -> None:
        self.assertEqual(mgo.parse_size("1000.0", "K"), 1024000)
        self.assertEqual(mgo.parse_size("0", "B"), 0)


class EventsDelta(unittest.TestCase):
    EV = {"high": 5, "max": 0, "oom": 0, "oom_kill": 0, "oom_group_kill": 0}

    def test_no_baseline(self) -> None:
        self.assertEqual(mgo.events_delta(None, "id:1", self.EV), (None, "no-baseline"))

    def test_same_cgroup_growth(self) -> None:
        prev = {"cgroup": "id:1", "events": dict(self.EV, high=2)}
        delta, basis = mgo.events_delta(prev, "id:1", self.EV)
        self.assertEqual((delta["high"], basis), (3, "same-cgroup"))

    def test_recreated_cgroup_counts_from_zero(self) -> None:
        prev = {"cgroup": "id:1", "events": dict(self.EV, high=900)}
        delta, basis = mgo.events_delta(prev, "id:2", self.EV)
        self.assertEqual((delta["high"], basis), (5, "cgroup-recreated"))

    def test_counter_went_backwards(self) -> None:
        prev = {"cgroup": "id:1", "events": dict(self.EV, high=9)}
        delta, basis = mgo.events_delta(prev, "id:1", self.EV)
        self.assertEqual((delta["high"], basis), (5, "counter-reset"))


class Verdicts(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.h = Harness(Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_pass_and_state_file_private(self) -> None:
        rc, r = self.h.run_json("--state-file", str(self.h.state))
        self.assertEqual((rc, r["verdict"]), (0, "PASS"))
        self.assertEqual(r["peak_pct_of_high"], 34.5)
        self.assertEqual(stat.S_IMODE(self.h.state.stat().st_mode), 0o600)
        saved = json.loads(self.h.state.read_text())
        self.assertEqual(saved[UNIT]["cgroup"], "id:4242")

    def test_high_growth_between_runs_warns(self) -> None:
        self.h.set_cgroup(high=100)
        self.h.run_json("--state-file", str(self.h.state))
        self.h.set_cgroup(high=130)
        rc, r = self.h.run_json("--state-file", str(self.h.state))
        self.assertEqual((rc, r["verdict"]), (1, "WARN"))
        self.assertEqual((r["events_delta"]["high"], r["delta_basis"]), (30, "same-cgroup"))

    def test_stale_high_on_same_cgroup_is_not_new_pressure(self) -> None:
        self.h.set_cgroup(high=21897)
        self.h.run_json("--state-file", str(self.h.state))
        rc, r = self.h.run_json("--state-file", str(self.h.state))
        self.assertEqual((rc, r["verdict"], r["events_delta"]["high"]), (0, "PASS", 0))

    def test_oom_kill_growth_fails(self) -> None:
        self.h.run_json("--state-file", str(self.h.state))
        self.h.set_cgroup(oom_kill=1)
        rc, r = self.h.run_json("--state-file", str(self.h.state))
        self.assertEqual((rc, r["verdict"]), (2, "FAIL"))

    def test_journal_oom_kill_fails(self) -> None:
        self.h.journal.write_text(f"1762000000.0 sogyo systemd[1]: {UNIT}: Failed with result 'oom-kill'.\n")
        rc, r = self.h.run_json()
        self.assertEqual((rc, r["verdict"], r["journal_oom_kills"]), (2, "FAIL", 1))

    def test_cycle_peak_over_threshold_and_reused_cgroup(self) -> None:
        self.h.journal.write_text(SOGYO_JOURNAL)
        rc, r = self.h.run_json()
        self.assertEqual((rc, r["verdict"]), (1, "WARN"))
        self.assertTrue(r["reused_cgroup"])
        self.assertTrue(r["counters_span_restarts"])
        self.assertEqual((r["stops_with_survivors"], r["starts_reusing_cgroup"]), (1, 1))
        self.assertEqual(r["window_max_cycle_peak"], int(1.9 * (1 << 30)))
        self.assertTrue(any("cycle peak" in reason for reason in r["reasons"]))

    def test_psi_some_avg300_warns(self) -> None:
        rc, r = self.h.run_json("--warn-pct", "100")
        self.assertEqual(r["verdict"], "PASS")
        self.h.set_cgroup(some300=12.5)
        rc, r = self.h.run_json()
        self.assertEqual((rc, r["verdict"], r["psi"]["some_avg300"]), (1, "WARN", 12.5))

    def test_inactive_unit_warns(self) -> None:
        self.h.set_show(active="inactive")
        rc, r = self.h.run_json()
        self.assertEqual((rc, r["verdict"]), (1, "WARN"))

    def test_systemctl_failure_unverified(self) -> None:
        self.h.show_rc.write_text("1")
        rc, r = self.h.run_json()
        self.assertEqual((rc, r["verdict"]), (3, "UNVERIFIED"))

    def test_missing_cgroup_files_unverified(self) -> None:
        (self.h.cg / "memory.events").unlink()
        rc, r = self.h.run_json()
        self.assertEqual((rc, r["verdict"]), (3, "UNVERIFIED"))

    def test_discovery_and_text_output_never_echo_journal_text(self) -> None:
        self.h.journal.write_text(SOGYO_JOURNAL)
        for args in ((), ("--json",)):
            rc, out, _ = self.h.run(*args)
            self.assertIn(UNIT, out)
            self.assertNotIn(SECRET_COMM, out)
            self.assertNotIn("Invalid argument", out)

    def test_usage_errors(self) -> None:
        self.assertEqual(self.h.run("--warn-pct", "0")[0], 64)
        self.assertEqual(self.h.run("--bogus")[0], 64)
        self.assertEqual(self.h.run("--since", "x", "--lookback-hours", "1")[0], 64)


if __name__ == "__main__":
    unittest.main()
