#!/usr/bin/env python3
"""Hermetic verdict tests for doctor's spans-rollup tick check (#2684).

`spans-rollup.py` exits 0 and delivers nothing on an idle day, so neither the
wiki queue nor the 09:00 digest can tell a quiet node from a cron line that
stopped firing. Every delivering run writes `spans-rollup.status.json`; these
tests pin that doctor reads it only when the cost-ledger block carries a
spans-rollup line, flags missing / stale / failed / unusable receipts, and
never echoes untrusted values from the file.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import json
import os
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ccc_doctor import Doctor  # noqa: E402

ITEM = "spans-rollup tick"
LEDGER_LINE = ("29 3 * * * bash -lc 'CCC_STATE_DIR=\"{state}\" python3 \"/x/cost-ledger.py\" "
               "--out \"{state}/cost-ledger.jsonl\"' >> \"{state}/cost-ledger.cron.log\" 2>&1  "
               "# ccc-node:cost-ledger gen=h_0123456789ab")
SPANS_LINE = ("41 3 * * * bash -lc 'CCC_STATE_DIR=\"{state}\" python3 \"/x/spans-rollup.py\"' "
              ">> \"{state}/cost-ledger.cron.log\" 2>&1  # ccc-node:cost-ledger gen=h_0123456789ab")


def stamp(age_hours: float) -> str:
    when = datetime.fromtimestamp(time.time() - age_hours * 3600, tz=timezone.utc)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def status(age_hours: float = 2, ok: bool = True, code: str = "queued") -> dict:
    return {"schema": "ccc.spans-rollup-status.v1", "finished_at": stamp(age_hours), "ok": ok,
            "result_code": code, "date": "2026-11-20", "node": "testnode"}


class SpansRollupTickCheck(unittest.TestCase):
    def run_check(self, payload: object | None = None, *, raw: str | None = None,
                  spans_line: bool = True, symlink: bool = False,
                  env: dict | None = None) -> Doctor:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            claude_dir = root / ".claude"
            # The cron line's CCC_STATE_DIR wins over the doctor's own state dir.
            state = root / "cron-state"
            state.mkdir(parents=True)
            (claude_dir / "state").mkdir(parents=True)
            lines = ["# ccc-node:cost-ledger:begin", LEDGER_LINE.format(state=state)]
            if spans_line:
                lines.append(SPANS_LINE.format(state=state))
            lines.append("# ccc-node:cost-ledger:end")
            cron_file = root / "crontab.txt"
            cron_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
            fake = root / "fake-crontab"
            fake.write_text(f"#!/bin/sh\ncat '{cron_file}'\n", encoding="utf-8")
            fake.chmod(0o755)
            path = state / "spans-rollup.status.json"
            if symlink:
                target = root / "elsewhere.json"
                target.write_text(json.dumps(status()), encoding="utf-8")
                path.symlink_to(target)
            elif raw is not None or payload is not None:
                path.write_text(raw if raw is not None else json.dumps(payload), encoding="utf-8")
            doctor = Doctor(Path.cwd(), claude_dir, "settings")
            with patch.dict("os.environ", {"CCC_CRONTAB_CMD": str(fake), **(env or {})},
                            clear=True):
                doctor.check_spans_rollup_tick()
            return doctor

    def assert_row(self, doctor: Doctor, klass: str, fragment: str):
        row = doctor.rows[-1]
        self.assertEqual(row.item, ITEM)
        self.assertEqual(row.klass, klass, f"status was: {row.status}")
        self.assertIn(fragment, row.status)
        return row

    def test_no_spans_line_is_not_installed(self) -> None:
        # The receipt is ignored when the opt-in --spans line is absent.
        doctor = self.run_check(status(age_hours=100), spans_line=False)
        self.assert_row(doctor, "정상", "not installed")

    def test_no_crontab_is_not_installed(self) -> None:
        doctor = Doctor(Path.cwd(), Path("/nonexistent/.claude"), "settings")
        with patch.dict("os.environ", {"CCC_CRONTAB_CMD": "/nonexistent/crontab"}, clear=True):
            doctor.check_spans_rollup_tick()
        self.assert_row(doctor, "정상", "not installed")

    def test_line_without_status_warns_missing(self) -> None:
        row = self.assert_row(self.run_check(None), "경고", "rollup-status-missing")
        self.assertIn("cron", row.action)

    def test_fresh_tick_is_normal(self) -> None:
        row = self.assert_row(self.run_check(status(age_hours=2)), "정상", "ok; result=queued")
        self.assertIn("age=2h", row.status)

    def test_fresh_no_spans_tick_is_normal(self) -> None:
        self.assert_row(self.run_check(status(code="no-spans")), "정상", "result=no-spans")

    def test_stale_tick_warns(self) -> None:
        row = self.assert_row(self.run_check(status(age_hours=40)), "경고", "rollup-tick-stale")
        self.assertIn("max=36h", row.status)

    def test_max_age_env_override(self) -> None:
        env = {"CCC_DOCTOR_SPANS_ROLLUP_MAX_AGE_HOURS": "48"}
        self.assert_row(self.run_check(status(age_hours=40), env=env), "정상", "ok;")
        env = {"CCC_DOCTOR_SPANS_ROLLUP_MAX_AGE_HOURS": "12"}
        self.assert_row(self.run_check(status(age_hours=13), env=env), "경고", "max=12h")

    def test_max_age_zero_disables_age_gate(self) -> None:
        env = {"CCC_DOCTOR_SPANS_ROLLUP_MAX_AGE_HOURS": "0"}
        self.assert_row(self.run_check(status(age_hours=500), env=env), "정상", "age=500h")

    def test_ok_false_warns_with_result_code(self) -> None:
        doctor = self.run_check(status(ok=False, code="sanitizer-unavailable"))
        self.assert_row(doctor, "경고", "rollup-failed; result=sanitizer-unavailable")

    def test_untrusted_values_are_not_echoed(self) -> None:
        payload = status(ok=False, code="Weird $(id)")
        payload["node"] = "evil $(id)"
        row = self.assert_row(self.run_check(payload), "경고", "result=unknown")
        self.assertNotIn("$(id)", row.status)
        self.assertNotIn("evil", row.status + row.action)

    def test_malformed_json_warns(self) -> None:
        self.assert_row(self.run_check(raw="{not json"), "경고", "rollup-status-malformed")

    def test_non_object_or_bad_timestamp_warns(self) -> None:
        self.assert_row(self.run_check(["ok"]), "경고", "rollup-status-malformed")
        bad = status()
        bad["finished_at"] = "yesterday"
        self.assert_row(self.run_check(bad), "경고", "rollup-status-malformed")

    def test_oversized_warns(self) -> None:
        raw = json.dumps({**status(), "pad": "x" * (70 * 1024)})
        self.assert_row(self.run_check(raw=raw), "경고", "rollup-status-oversized")

    def test_symlink_warns(self) -> None:
        self.assert_row(self.run_check(symlink=True), "경고", "rollup-status-symlink")

    @unittest.skipIf(os.geteuid() == 0, "root bypasses file permissions")
    def test_unreadable_is_manual(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / "state"
            state.mkdir()
            path = state / "spans-rollup.status.json"
            path.write_text(json.dumps(status()), encoding="utf-8")
            path.chmod(0)
            cron_file = root / "crontab.txt"
            cron_file.write_text(SPANS_LINE.format(state=state) + "\n", encoding="utf-8")
            fake = root / "fake-crontab"
            fake.write_text(f"#!/bin/sh\ncat '{cron_file}'\n", encoding="utf-8")
            fake.chmod(0o755)
            doctor = Doctor(Path.cwd(), root / ".claude", "settings")
            with patch.dict("os.environ", {"CCC_CRONTAB_CMD": str(fake)}, clear=True):
                doctor.check_spans_rollup_tick()
            path.chmod(0o600)
        self.assert_row(doctor, "수동필요", "rollup-status-unreadable")

    def test_unreadable_is_manual_via_oserror(self) -> None:
        with patch.object(Path, "read_text", side_effect=PermissionError("denied")):
            doctor = self.run_check(status())
        self.assert_row(doctor, "수동필요", "rollup-status-unreadable")

    def test_falls_back_to_doctor_state_dir_without_baked_dir(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            claude_dir = root / ".claude"
            (claude_dir / "state").mkdir(parents=True)
            (claude_dir / "state" / "spans-rollup.status.json").write_text(
                json.dumps(status()), encoding="utf-8")
            cron_file = root / "crontab.txt"
            cron_file.write_text("41 3 * * * python3 /x/spans-rollup.py  "
                                 "# ccc-node:cost-ledger gen=h_0123456789ab\n", encoding="utf-8")
            fake = root / "fake-crontab"
            fake.write_text(f"#!/bin/sh\ncat '{cron_file}'\n", encoding="utf-8")
            fake.chmod(0o755)
            doctor = Doctor(Path.cwd(), claude_dir, "settings")
            with patch.dict("os.environ", {"CCC_CRONTAB_CMD": str(fake)}, clear=True):
                doctor.check_spans_rollup_tick()
        self.assert_row(doctor, "정상", "ok; result=queued")


if __name__ == "__main__":
    unittest.main()
