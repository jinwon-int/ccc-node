#!/usr/bin/env python3
"""Unit tests for journal_silence_watch.

The motivating outage: a phone automation app's task queue hung at 17:49 and
the webhook receiver logged no request for ~16 hours while every server-side
health check stayed green. These tests pin the change-based paging (NEW once,
STILL on a cadence, RECOVERED once), the UNVERIFIED path for an unreadable
journal, and that matched journal text never reaches the page.

Run standalone: python3 scripts/journal_silence_watch_test.py
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

import journal_silence_watch as jsw

NOW = 1_791_767_000.0  # fixed epoch for deterministic runs
BODY_MARKER = "SENSITIVE_BODY_MARKER 1,234원 승인"


class Harness:
    """A fake journalctl whose output is the content of ``lines``."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.journal = root / "journal.txt"
        self.rc = root / "rc"
        self.args_log = root / "args"
        self.bin = root / "journalctl"
        self.bin.write_text(
            "#!/usr/bin/env bash\n"
            f'printf "%s\\n" "$*" > "{self.args_log}"\n'
            f'cat "{self.journal}"\n'
            f'exit "$(cat "{self.rc}" 2>/dev/null || echo 0)"\n',
            encoding="utf-8",
        )
        self.bin.chmod(0o755)
        self.state = root / "state" / "watch.json"
        self.set_lines([])

    def set_lines(self, entries: list[tuple[float, str]], rc: int = 0) -> None:
        self.journal.write_text(
            "".join(f"{ts:.6f} host python[1]: {msg}\n" for ts, msg in entries),
            encoding="utf-8",
        )
        self.rc.write_text(str(rc), encoding="utf-8")

    def run(self, now: float, *extra: str) -> tuple[int, str]:
        argv = [
            "--unit", "receiver.service", "--pattern", r"POST /notification",
            "--state-file", str(self.state), "--max-silence-min", "30",
            "--still-every-min", "180", "--now", str(now), "--label", "receiver",
            *extra,
        ]
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"CCC_JOURNAL_SILENCE_JOURNALCTL": str(self.bin)}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = jsw.main(argv)
        return rc, out.getvalue()


def req(ts: float) -> tuple[float, str]:
    return ts, f'[알림] {BODY_MARKER} INFO: 1.2.3.4:0 - "POST /notification HTTP/1.1" 200'


class JournalSilenceWatchTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.h = Harness(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_recent_match_is_quiet_ok(self) -> None:
        self.h.set_lines([req(NOW - 300)])
        rc, out = self.h.run(NOW)
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("OK receiver"), out)

    def test_only_matching_lines_count(self) -> None:
        # Fresh unrelated lines (e.g. GET /docs probes) must not mask silence.
        self.h.set_lines([req(NOW - 7200), (NOW - 10, 'INFO: "GET /docs HTTP/1.1" 200')])
        rc, out = self.h.run(NOW)
        self.assertEqual(rc, 1)
        self.assertTrue(out.startswith("NEW receiver 무수신 2시간 0분"), out)

    def test_silence_pages_new_then_quiet_then_still(self) -> None:
        self.h.set_lines([req(NOW - 3600)])
        rc, out = self.h.run(NOW)
        self.assertEqual(rc, 1)
        self.assertIn("NEW receiver", out)
        self.assertIn("기준 30분", out)
        rc, out = self.h.run(NOW + 900)
        self.assertEqual(rc, 0, out)
        self.assertTrue(out.startswith("SILENT receiver"), out)
        rc, out = self.h.run(NOW + 180 * 60)
        self.assertEqual(rc, 1)
        self.assertTrue(out.startswith("STILL receiver"), out)
        rc, _ = self.h.run(NOW + 181 * 60)
        self.assertEqual(rc, 0)

    def test_recovery_pages_once_with_outage_span(self) -> None:
        self.h.set_lines([req(NOW - 3600)])
        self.assertEqual(self.h.run(NOW)[0], 1)
        self.h.set_lines([req(NOW - 3600), req(NOW + 5400 - 60)])
        rc, out = self.h.run(NOW + 5400)
        self.assertEqual(rc, 1)
        self.assertTrue(out.startswith("RECOVERED receiver"), out)
        self.assertIn("무수신 경보 지속 1시간 30분", out)
        rc, out = self.h.run(NOW + 5700)
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("OK receiver"), out)

    def test_no_match_in_lookback_pages_new(self) -> None:
        rc, out = self.h.run(NOW)
        self.assertEqual(rc, 1)
        self.assertIn("무수신 72시간 이상", out)
        self.assertIn("마지막 일치 -", out)

    def test_unreadable_journal_is_unverified_not_silence(self) -> None:
        self.h.set_lines([req(NOW - 60)], rc=1)
        rc, out = self.h.run(NOW)
        self.assertEqual(rc, 1)
        self.assertTrue(out.startswith("NEW UNVERIFIED receiver"), out)
        self.assertIn("journalctl exit 1", out)
        rc, out = self.h.run(NOW + 600)
        self.assertEqual(rc, 0, out)
        self.h.set_lines([req(NOW + 1100)])
        rc, out = self.h.run(NOW + 1200)
        self.assertEqual(rc, 1)
        self.assertTrue(out.startswith("RECOVERED"), out)

    def test_missing_journalctl_is_unverified(self) -> None:
        self.h.bin.unlink()
        rc, out = self.h.run(NOW)
        self.assertEqual(rc, 1)
        self.assertIn("UNVERIFIED", out)
        self.assertIn("failed to start", out)

    def test_page_never_leaks_matched_text(self) -> None:
        self.h.set_lines([req(NOW - 3600)])
        outputs = [self.h.run(NOW)[1]]
        self.h.set_lines([req(NOW + 60)])
        outputs.append(self.h.run(NOW + 120)[1])
        outputs.append(self.h.run(NOW + 240)[1])
        for out in outputs:
            self.assertNotIn("SENSITIVE_BODY_MARKER", out)
            self.assertNotIn("1,234", out)
        self.assertNotIn("SENSITIVE_BODY_MARKER", self.h.state.read_text(encoding="utf-8"))

    def test_state_file_is_owner_only_and_versioned(self) -> None:
        self.h.set_lines([req(NOW - 60)])
        self.h.run(NOW)
        mode = stat.S_IMODE(self.h.state.stat().st_mode)
        self.assertEqual(mode, 0o600)
        data = json.loads(self.h.state.read_text(encoding="utf-8"))
        self.assertEqual(data["schema"], jsw.SCHEMA)
        self.assertEqual(data["state"], "ok")

    def test_corrupt_state_is_treated_as_fresh(self) -> None:
        self.h.state.parent.mkdir(parents=True)
        self.h.state.write_text("{not json", encoding="utf-8")
        self.h.set_lines([req(NOW - 60)])
        rc, out = self.h.run(NOW)
        self.assertEqual(rc, 0, out)

    def test_journal_query_window_uses_lookback(self) -> None:
        self.h.set_lines([req(NOW - 60)])
        self.h.run(NOW, "--lookback-hours", "2")
        args = self.h.args_log.read_text(encoding="utf-8").split()
        self.assertIn("receiver.service", args)
        self.assertIn(f"@{int(NOW - 7200)}", args)
        self.assertIn("short-unix", args)

    def test_lines_after_now_are_ignored(self) -> None:
        # Replaying a past moment (--now in the past) must not see requests
        # that arrived later; found replaying the motivating outage live.
        self.h.set_lines([req(NOW - 3600), req(NOW + 600)])
        rc, out = self.h.run(NOW)
        self.assertEqual(rc, 1)
        self.assertTrue(out.startswith("NEW receiver 무수신 1시간 0분"), out)
        args = self.h.args_log.read_text(encoding="utf-8").split()
        self.assertIn(f"@{int(NOW) + 1}", args)

    def test_usage_errors_exit_2(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(jsw.main(["--unit", "x"]), 2)
            self.assertEqual(jsw.main([
                "--unit", "x", "--pattern", "(", "--state-file", str(self.h.state)]), 2)
            self.assertEqual(jsw.main([
                "--unit", "x", "--pattern", "a", "--state-file", str(self.h.state),
                "--max-silence-min", "0"]), 2)


if __name__ == "__main__":
    unittest.main()
