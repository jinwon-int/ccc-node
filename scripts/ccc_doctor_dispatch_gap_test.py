#!/usr/bin/env python3
"""Hermetic verdict tests for doctor's skill-promotion dispatch-gap check.

The publish path hands an intake PR to an A2A reviewer exactly once, when the
PR opens. Until 2026-10-07 a skipped dispatch was final and invisible: the
cron log kept a 500-byte digest, the ledger got no row, and nothing retried.
Field case 2026-10-05~07: a T1 edge-secret rotation missed the publisher's
env file; every dispatch skipped with `dispatch_broker_unreachable`; nine
intake PRs waited three days with no alert. These tests pin the properties
that make the ledger-based report trustworthy: absence is not drift, a
dispatched/young PR is not a gap, the verdict names the latest skip code with
a cause-specific hint, and an oversized ledger degrades to 경고 instead of
scanning forever.
"""

from __future__ import annotations

import json
import os
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ccc_doctor import Doctor  # noqa: E402

HOUR = 3600


def iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


class DispatchGapCheck(unittest.TestCase):
    def run_check(self, rows: list[dict] | None, *, oversized: bool = False) -> Doctor:
        with TemporaryDirectory() as temp:
            claude_dir = Path(temp) / ".claude"
            state = claude_dir / "state"
            state.mkdir(parents=True)
            if rows is not None:
                prom = state / "skill-promotion"
                prom.mkdir()
                ledger = prom / "ledger.jsonl"
                ledger.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
                if oversized:
                    with ledger.open("ab") as handle:
                        handle.truncate(Doctor._DISPATCH_GAP_LEDGER_MAX_BYTES + 1)
            doctor = Doctor.__new__(Doctor)
            doctor.claude_dir = claude_dir
            doctor.rows = []
            doctor.counts = {"정상": 0, "경고": 0, "수동필요": 0, "자동수정가능": 0}
            with patch.dict(os.environ, {"CCC_STATE_DIR": str(state)}):
                doctor.check_skill_promotion_dispatch_gap()
            return doctor

    def row_for(self, doctor: Doctor):
        rows = [r for r in doctor.rows if r.item == "skill-promotion dispatch gap"]
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_absent_ledger_is_not_drift(self) -> None:
        row = self.row_for(self.run_check(None))
        self.assertEqual(row.klass, "정상")
        self.assertEqual(row.status, "ledger=absent")

    def test_dispatched_intake_is_healthy(self) -> None:
        now = time.time()
        branch = "skill-intake/nodea/alpha-claude-" + "a" * 12
        rows = [
            {"ts": iso(now - 30 * HOUR), "outcome": "pr-opened", "branch": branch, "url": "u"},
            {"ts": iso(now - 29 * HOUR), "kind": "a2a-dispatch", "branch": branch, "head_sha": "a" * 40},
        ]
        row = self.row_for(self.run_check(rows))
        self.assertEqual(row.klass, "정상")
        self.assertEqual(row.status, "intake=1; undispatched=0")

    def test_young_undispatched_pr_is_left_to_publish_path(self) -> None:
        now = time.time()
        rows = [{"ts": iso(now - 10 * 60), "outcome": "pr-opened", "branch": "skill-intake/nodea/alpha-claude-" + "a" * 12}]
        row = self.row_for(self.run_check(rows))
        self.assertEqual(row.klass, "정상")

    def test_gap_names_latest_skip_code_and_hint(self) -> None:
        now = time.time()
        rows = [
            {"ts": iso(now - 50 * HOUR), "outcome": "pr-opened", "branch": "skill-intake/nodea/alpha-claude-" + "a" * 12},
            {"ts": iso(now - 26 * HOUR), "outcome": "pr-opened", "branch": "skill-intake/nodeb/beta-claude-" + "b" * 12},
            {"ts": iso(now - 49 * HOUR), "kind": "a2a-dispatch-skipped", "code": "dispatch_ci_not_green"},
            {"ts": iso(now - 25 * HOUR), "kind": "a2a-dispatch-skipped", "code": "dispatch_broker_unreachable"},
            {"ts": iso(now - 3 * HOUR), "outcome": "pr-opened", "branch": "skill-intake/nodec/gamma-danso-" + "c" * 12},
            {"ts": iso(now - 2 * HOUR), "kind": "a2a-dispatch", "branch": "skill-intake/nodec/gamma-danso-" + "c" * 12},
        ]
        row = self.row_for(self.run_check(rows))
        self.assertEqual(row.klass, "경고")
        self.assertEqual(row.status, "undispatched=2/3; oldest=50h; last_skip=dispatch_broker_unreachable")
        self.assertIn("a2a-broker-edge.env", row.action)
        self.assertIn("RB-990", row.action)

    def test_unknown_code_gets_generic_hint(self) -> None:
        now = time.time()
        rows = [
            {"ts": iso(now - 5 * HOUR), "outcome": "pr-opened", "branch": "skill-intake/nodea/alpha-claude-" + "a" * 12},
            {"ts": iso(now - 4 * HOUR), "kind": "a2a-dispatch-skipped", "code": "dispatch_round_failed"},
        ]
        row = self.row_for(self.run_check(rows))
        self.assertEqual(row.klass, "경고")
        self.assertIn("last_skip=dispatch_round_failed", row.status)
        self.assertIn("last-collect.json", row.action)

    def test_window_excludes_old_intakes(self) -> None:
        now = time.time()
        rows = [{"ts": iso(now - 20 * 86400), "outcome": "pr-opened", "branch": "skill-intake/nodea/old-claude-" + "a" * 12}]
        row = self.row_for(self.run_check(rows))
        self.assertEqual(row.klass, "정상")
        self.assertTrue(row.status.startswith("intake=0"))

    def test_oversized_ledger_degrades_to_warning(self) -> None:
        row = self.row_for(self.run_check([], oversized=True))
        self.assertEqual(row.klass, "경고")
        self.assertEqual(row.status, "ledger=oversized")

    def test_never_escalates_past_warning(self) -> None:
        now = time.time()
        rows = [{"ts": iso(now - 300 * HOUR), "outcome": "pr-opened", "branch": "skill-intake/nodea/alpha-claude-" + "a" * 12}]
        row = self.row_for(self.run_check(rows))
        self.assertEqual(row.klass, "경고")


if __name__ == "__main__":
    unittest.main()
