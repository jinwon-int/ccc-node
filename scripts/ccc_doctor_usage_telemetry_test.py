#!/usr/bin/env python3
"""Hermetic verdict tests for doctor's skill-usage telemetry check (#1675).

The retirement audit (#1648) reads an empty usage ledger as "this skill is
unused". That inference is only sound if a recording path is known to work,
and nothing checked: `curator-bump.sh` swallowed every failure and
`_command_bump` collapsed all of them into a bare degraded=True, so a broken
hook and an idle node produced the same observable state -- an empty ledger.

Diagnosing one node cost a hook/jq/python3/permission sweep, a grep of 644
session transcripts, and a control run on a healthy node to discover that the
first repro was a temp-dir artifact. None of that should have been necessary.

These tests pin what makes the report trustworthy: an unwired node is not
drift, a failing recording path is reported as a defect on its own terms and
never folded into the staleness verdict, an unparsable stamp never reads as
"brand new", and no verdict escalates past 경고 -- a node being idle is not a
locally repairable fault and must not block `doctor --fix`.
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

ITEM = "skill-usage telemetry"


def ago(days: float) -> str:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


class UsageTelemetryCheck(unittest.TestCase):
    def run_check(
        self,
        *,
        hook: bool = True,
        rows: list[dict] | None = None,
        degraded: list[str] | None = None,
        ledger_mode: int = 0o600,
    ) -> Doctor:
        """rows None => no ledger file at all; [] => ledger present but empty."""
        with TemporaryDirectory() as temp:
            claude_dir = Path(temp) / ".claude"
            state = claude_dir / "state"
            state.mkdir(parents=True)
            if hook:
                hooks = claude_dir / "hooks"
                hooks.mkdir(parents=True)
                (hooks / "skill-usage-log.sh").write_text("#!/bin/sh\n", encoding="utf-8")
            usage = state / "skill-usage"
            if rows is not None or degraded is not None:
                usage.mkdir(parents=True)
            if degraded is not None:
                (usage / "degraded.log").write_text(
                    "".join(line + "\n" for line in degraded), encoding="utf-8")
            if rows is not None:
                ledger = usage / "usage.jsonl"
                ledger.write_text(
                    "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
                ledger.chmod(ledger_mode)
            doctor = Doctor(Path.cwd(), claude_dir, "settings")
            with patch.dict("os.environ", {}, clear=True):
                doctor.check_skill_usage_telemetry()
            return doctor

    def assert_row(self, doctor: Doctor, klass: str, fragment: str) -> None:
        row = doctor.rows[-1]
        self.assertEqual(row.item, ITEM)
        self.assertEqual(row.klass, klass, f"status was: {row.status}")
        self.assertIn(fragment, row.status)

    # --- absence is not drift ----------------------------------------------

    def test_unwired_node_is_normal(self) -> None:
        """A node without the Claude Code harness has no hook; that is fine."""
        self.assert_row(self.run_check(hook=False), "정상", "hook=absent")

    def test_unwired_node_stays_normal_even_with_stale_leftovers(self) -> None:
        """No hook means no expectation of records, whatever is on disk."""
        self.assert_row(
            self.run_check(hook=False, rows=[{"skill": "a", "ts": ago(90)}]),
            "정상",
            "hook=absent",
        )

    # --- a failing recording path is its own finding -----------------------

    def test_degraded_entries_warn(self) -> None:
        self.assert_row(
            self.run_check(degraded=[f"{ago(1)} bump curator:contract:skill_missing"]),
            "경고",
            "degraded=1",
        )

    def test_degraded_wins_over_a_healthy_ledger(self) -> None:
        """The two answer different questions; a fresh ledger must not mask a
        failing path, because the audit's inference depends on the path."""
        doctor = self.run_check(
            rows=[{"skill": "a", "ts": ago(0)}],
            degraded=[f"{ago(0)} bump wrapper:jq_missing"],
        )
        self.assert_row(doctor, "경고", "degraded=1")

    def test_degraded_reason_is_surfaced(self) -> None:
        """A bare count would repeat the defect this check exists to fix."""
        doctor = self.run_check(degraded=[f"{ago(0)} bump wrapper:python3_missing"])
        self.assertIn("python3_missing", doctor.rows[-1].status)

    def test_empty_degraded_log_is_not_a_finding(self) -> None:
        self.assert_row(
            self.run_check(degraded=[], rows=[{"skill": "a", "ts": ago(0)}]),
            "정상",
            "newest=0d",
        )

    # --- wired but never recorded ------------------------------------------

    def test_wired_without_ledger_warns(self) -> None:
        """Exactly the sogyo shape: hook installed, nothing ever recorded."""
        self.assert_row(self.run_check(rows=None), "경고", "ledger=absent")

    def test_ledger_with_no_usable_row_warns(self) -> None:
        self.assert_row(self.run_check(rows=[]), "경고", "ledger=empty")

    # --- staleness follows the NEWEST record -------------------------------

    def test_recent_record_is_normal(self) -> None:
        self.assert_row(self.run_check(rows=[{"skill": "a", "ts": ago(1)}]), "정상", "newest=1d")

    def test_silent_ledger_warns(self) -> None:
        self.assert_row(self.run_check(rows=[{"skill": "a", "ts": ago(30)}]), "경고", "newest=30d")

    def test_one_recent_record_clears_many_old_ones(self) -> None:
        rows = [{"skill": "a", "ts": ago(90)} for _ in range(20)]
        rows.append({"skill": "b", "ts": ago(0)})
        self.assert_row(self.run_check(rows=rows), "정상", "newest=0d")

    def test_boundary_is_the_documented_window(self) -> None:
        self.assertEqual(Doctor._TELEMETRY_SILENT_DAYS, 14)
        self.assert_row(self.run_check(rows=[{"skill": "a", "ts": ago(13.5)}]), "정상", "newest=13d")
        self.assert_row(self.run_check(rows=[{"skill": "a", "ts": ago(14.5)}]), "경고", "newest=14d")

    # --- malformed input never reads as healthy ----------------------------

    def test_unparsable_stamp_does_not_read_as_brand_new(self) -> None:
        """_iso_age_days returns None, not 0 -- the verdict must say so rather
        than silently claiming a fresh record."""
        doctor = self.run_check(rows=[{"skill": "a", "ts": "not-a-timestamp"}])
        self.assert_row(doctor, "정상", "newest=unparsable")

    def test_corrupt_lines_are_skipped_not_fatal(self) -> None:
        with TemporaryDirectory() as temp:
            claude_dir = Path(temp) / ".claude"
            (claude_dir / "hooks").mkdir(parents=True)
            (claude_dir / "hooks" / "skill-usage-log.sh").write_text("#!/bin/sh\n", encoding="utf-8")
            usage = claude_dir / "state" / "skill-usage"
            usage.mkdir(parents=True)
            (usage / "usage.jsonl").write_text(
                "{not json\n\n" + json.dumps({"skill": "a", "ts": ago(0)}) + "\n",
                encoding="utf-8",
            )
            doctor = Doctor(Path.cwd(), claude_dir, "settings")
            with patch.dict("os.environ", {}, clear=True):
                doctor.check_skill_usage_telemetry()
        self.assert_row(doctor, "정상", "newest=0d")

    # --- never escalates past 경고 -----------------------------------------

    def test_no_verdict_is_repairable_drift(self) -> None:
        """An idle node is not a local fault; escalating would block --fix."""
        for doctor in (
            self.run_check(rows=None),
            self.run_check(rows=[]),
            self.run_check(rows=[{"skill": "a", "ts": ago(60)}]),
            self.run_check(degraded=[f"{ago(0)} bump wrapper:jq_missing"]),
        ):
            self.assertIn(doctor.rows[-1].klass, {"정상", "경고"})


if __name__ == "__main__":
    unittest.main()
