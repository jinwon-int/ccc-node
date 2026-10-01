#!/usr/bin/env python3
"""Hermetic tests for doctor's read-only "session scope rows" check (#2075).

#2092 fixed how the bridge's runners pick a session; rows filled with a
DM-derived session id before that fix keep resuming it inside a group. The
doctor row surfaces the count (never keys or session ids), stays a warning
(exit code unchanged), points at the operator-run audit tool, and never
writes the store.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ccc_doctor import Doctor  # noqa: E402

ITEM = "session scope rows"
DM_SID = "dm-session-0001"


def write_store(path: Path, data: dict) -> bytes:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = (json.dumps(data, indent=2) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(payload)
    return payload


def contaminated() -> dict:
    return {
        "telegram_session:7": {"session_id": DM_SID},
        "telegram_session:7:-100": {"session_id": DM_SID},
        "telegram_session:0:-200": {"session_id": DM_SID},
    }


class SessionScopeRows(unittest.TestCase):
    def run_check(self, telegram: dict | None, matrix: dict | None = None, raw: bytes | None = None):
        with TemporaryDirectory() as temp:
            home = Path(temp)
            payloads = {}
            if telegram is not None:
                payloads["tg"] = write_store(home / ".telegram_bot" / "sessions.json", telegram)
            if raw is not None:
                (home / ".telegram_bot").mkdir(mode=0o700, exist_ok=True)
                fd = os.open(home / ".telegram_bot" / "sessions.json", os.O_WRONLY | os.O_CREAT, 0o600)
                os.write(fd, raw)
                os.close(fd)
            if matrix is not None:
                payloads["mx"] = write_store(home / ".ccc-matrix" / "sessions.json", matrix)
            doctor = Doctor(Path(__file__).resolve().parents[1], home / ".claude", "settings")
            env = {"HOME": temp, "CCC_DOCTOR_BRIDGE_PROJECT_ROOT": temp}
            with patch.dict("os.environ", env, clear=True), patch.object(
                Doctor, "running_bridge_home", return_value=None
            ):
                doctor.check_session_scope_rows()
            if "tg" in payloads:
                self.assertEqual(
                    (home / ".telegram_bot" / "sessions.json").read_bytes(), payloads["tg"]
                )
            self.assertEqual(sorted(p.name for p in home.glob("*/sessions.json.bak*")), [])
            return doctor

    def row(self, doctor: Doctor):
        rows = [r for r in doctor.rows if r.item == ITEM]
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_no_store_means_no_row(self):
        doctor = self.run_check(None)
        self.assertEqual([r for r in doctor.rows if r.item == ITEM], [])

    def test_clean_store_is_normal(self):
        row = self.row(self.run_check({"telegram_session:7": {"session_id": "a"}}))
        self.assertEqual(row.klass, "정상")
        self.assertEqual(row.status, "stores=1 flagged=0")

    def test_contaminated_rows_warn_with_counts_only(self):
        doctor = self.run_check(contaminated(), matrix=contaminated())
        row = self.row(doctor)
        self.assertEqual(row.klass, "경고")
        self.assertIn("DEFECT: 4 group/room session row(s)", row.status)
        self.assertIn("stores=2", row.status)
        self.assertIn("ccc_session_scope_audit.py", row.action)
        self.assertIn("--apply", row.action)
        for text in (row.status, row.action):
            self.assertNotIn(DM_SID, text)
            self.assertNotIn("-100", text)
            self.assertNotIn("-200", text)
        self.assertEqual(doctor.report_exit_code(), 0)

    def test_bot_data_dir_adds_to_the_project_stores(self):
        with TemporaryDirectory() as temp:
            home = Path(temp)
            write_store(home / ".telegram_bot" / "sessions.json", {"telegram_session:7": {}})
            write_store(home / "matrix-data" / "sessions.json", contaminated())
            doctor = Doctor(Path(__file__).resolve().parents[1], home / ".claude", "settings")
            env = {
                "HOME": temp,
                "CCC_DOCTOR_BRIDGE_PROJECT_ROOT": temp,
                "BOT_DATA_DIR": str(home / "matrix-data"),
            }
            with patch.dict("os.environ", env, clear=True), patch.object(
                Doctor, "running_bridge_home", return_value=None
            ):
                doctor.check_session_scope_rows()
        row = self.row(doctor)
        self.assertEqual(row.klass, "경고")
        self.assertIn("DEFECT: 2 group/room", row.status)
        self.assertIn("stores=2", row.status)

    def test_live_bridge_of_another_checkout_is_not_read(self):
        with TemporaryDirectory() as temp:
            live_home = Path(temp) / "live"
            write_store(live_home / ".telegram_bot" / "sessions.json", contaminated())
            doctor = Doctor(Path(__file__).resolve().parents[1], Path(temp) / "fixture" / ".claude", "settings")
            with patch.dict("os.environ", {"HOME": temp}, clear=True), patch.object(
                Doctor, "running_bridge_root", return_value=str(Path(temp) / "other-checkout")
            ), patch.object(Doctor, "running_bridge_home", return_value=str(live_home)):
                doctor.check_session_scope_rows()
            self.assertEqual([r for r in doctor.rows if r.item == ITEM], [])

    def test_unreadable_store_warns_without_failing_exit(self):
        doctor = self.run_check(None, raw=b"{not json")
        row = self.row(doctor)
        self.assertEqual(row.klass, "경고")
        self.assertIn("unreadable store(s)=1 of 1", row.status)
        self.assertEqual(doctor.report_exit_code(), 0)


if __name__ == "__main__":
    unittest.main()
