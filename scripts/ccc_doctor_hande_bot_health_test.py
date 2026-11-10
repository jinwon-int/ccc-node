#!/usr/bin/env python3
"""Hermetic verdict tests for doctor's hande bot health check (#2380).

The native relay bot (family-messenger#739) writes ``<rooms-dir>/health.json``
(schema v1): relay reachability, the last 403 that ended its loop, and each
room's halt state. Before it, those signals lived only in the bot's journal.
These tests pin the doctor's reading of that file: a halted room needs the
operator, a stale file / long outage / recent 403 warns, a one-retry relay
blip does not, and the check stays quiet on a node that runs no bot.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import json
import os
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ccc_doctor  # noqa: E402
from ccc_doctor import Doctor  # noqa: E402

ITEM = "hande bot health"


def body(now: float, **over) -> dict:
    data = {
        "version": 1,
        "device": "bot-1",
        "pid": 4242,
        "started": int(now) - 600,
        "updated": int(now) - 5,
        "relay": {"state": "up", "since": int(now) - 600, "attempts": 0, "last_error_class": None, "outages": 0},
        "access": {"last_403": None},
        "rooms": [
            {"room": "family", "joined": True, "mode": "agent", "halted": False, "halt_reason": None,
             "pending": False, "cursor": 12, "epoch": 3},
        ],
    }
    data.update(over)
    return data


class HandeBotHealthCheck(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        self.base = Path(self._temp.name)
        self.claude_dir = self.base / ".claude"
        self.claude_dir.mkdir()
        self.health = self.base / "rooms" / "health.json"
        self.health.parent.mkdir()
        self.now = time.time()

    def tearDown(self) -> None:
        self._temp.cleanup()

    def write(self, data: dict | str, path: Path | None = None) -> Path:
        path = path or self.health
        path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
        return path

    def declare_hande(self) -> None:
        (self.claude_dir / "frontends").write_text("# test\nhande\n", encoding="utf-8")

    def run_check(self, env: dict[str, str] | None = None, paths: list[Path] | None = None) -> Doctor:
        doctor = Doctor(Path.cwd(), self.claude_dir, "settings")
        # Never read the host's real bot files from a test.
        merged = {"CCC_DOCTOR_HANDE_BOT_HEALTH": ",".join(str(p) for p in (paths or [self.health])), **(env or {})}
        with patch.dict("os.environ", merged, clear=True):
            doctor.check_hande_bot_health()
        return doctor

    def rows(self, doctor: Doctor):
        rows = [r for r in doctor.rows if r.item.startswith(ITEM)]
        self.assertTrue(rows, [r.item for r in doctor.rows])
        return rows

    def row(self, doctor: Doctor):
        rows = self.rows(doctor)
        self.assertEqual(len(rows), 1, [(r.item, r.status) for r in rows])
        return rows[0]

    # -- applicability -----------------------------------------------------

    def test_no_file_and_no_hande_frontend_is_not_applicable(self) -> None:
        row = self.row(self.run_check())
        self.assertEqual(row.klass, "정상")
        self.assertIn("해당 없음", row.status)

    def test_no_file_on_a_hande_node_warns(self) -> None:
        self.declare_hande()
        row = self.row(self.run_check())
        self.assertEqual(row.klass, "경고")
        self.assertIn("missing", row.status)
        self.assertIn("#739", row.action)

    def test_frontends_env_declares_hande_too(self) -> None:
        row = self.row(self.run_check(env={"CCC_FRONTENDS": "hande"}))
        self.assertEqual(row.klass, "경고")

    # -- verdicts ----------------------------------------------------------

    def test_healthy_bot_is_normal_with_a_summary(self) -> None:
        self.write(body(self.now))
        row = self.row(self.run_check())
        self.assertEqual(row.klass, "정상", row.status)
        self.assertIn("relay=up", row.status)
        self.assertIn("rooms=1", row.status)
        self.assertNotIn("age=", row.status, "a healthy line carries no clock-derived value")
        self.assertEqual(row.action, "none")

    def test_halted_room_needs_the_operator_with_its_reason(self) -> None:
        rooms = [
            {"room": "family", "joined": True, "mode": "agent", "halted": True, "halt_reason": "history_gap",
             "pending": False, "cursor": 12, "epoch": 3},
            {"room": "other", "joined": True, "mode": "silent", "halted": False, "halt_reason": None,
             "pending": False, "cursor": 1, "epoch": 1},
        ]
        self.write(body(self.now, rooms=rooms))
        row = self.row(self.run_check())
        self.assertEqual(row.klass, "수동필요")
        self.assertIn("halted=1", row.status)
        self.assertIn("family(history_gap)", row.status)
        self.assertIn(".state.refused", row.action)

    def test_halt_outranks_staleness(self) -> None:
        rooms = [{"room": "family", "halted": True, "halt_reason": "commit_refused"}]
        self.write(body(self.now, rooms=rooms, updated=int(self.now) - 3600))
        self.assertEqual(self.row(self.run_check()).klass, "수동필요")

    def test_stale_file_warns_and_threshold_is_configurable(self) -> None:
        self.write(body(self.now, updated=int(self.now) - 400))
        row = self.row(self.run_check())
        self.assertEqual(row.klass, "경고")
        self.assertIn("stale", row.status)
        row = self.row(self.run_check(env={"CCC_DOCTOR_HANDE_BOT_STALE_SECONDS": "900"}))
        self.assertEqual(row.klass, "정상", row.status)

    def test_missing_updated_is_stale(self) -> None:
        data = body(self.now)
        del data["updated"]
        self.write(data)
        row = self.row(self.run_check())
        self.assertEqual(row.klass, "경고")
        self.assertIn("age=?", row.status)

    def test_relay_outage_past_a_minute_warns(self) -> None:
        relay = {"state": "unreachable", "since": int(self.now) - 120, "attempts": 7,
                 "last_error_class": "timeout", "outages": 3}
        self.write(body(self.now, relay=relay))
        row = self.row(self.run_check())
        self.assertEqual(row.klass, "경고")
        self.assertIn("relay unreachable for", row.status)
        self.assertIn("last_error=timeout", row.status)

    def test_one_retry_blip_does_not_warn(self) -> None:
        relay = {"state": "unreachable", "since": int(self.now) - 2, "attempts": 1,
                 "last_error_class": "read_timeout", "outages": 9}
        self.write(body(self.now, relay=relay))
        row = self.row(self.run_check())
        self.assertEqual(row.klass, "정상", row.status)
        self.assertIn("outages=9", row.status)

    def test_recent_403_warns_and_an_old_one_does_not(self) -> None:
        self.write(body(self.now, access={"last_403": {"code": "device_not_allowed", "at": int(self.now) - 120}}))
        row = self.row(self.run_check())
        self.assertEqual(row.klass, "경고")
        self.assertIn("403 device_not_allowed", row.status)
        self.write(body(self.now, access={"last_403": {"code": "device_not_allowed", "at": int(self.now) - 7200}}))
        self.assertEqual(self.row(self.run_check()).klass, "정상")

    def test_unknown_schema_warns(self) -> None:
        self.write(body(self.now, version=2))
        row = self.row(self.run_check())
        self.assertEqual(row.klass, "경고")
        self.assertIn("unknown schema", row.status)
        self.write("[1, 2]")
        self.assertEqual(self.row(self.run_check()).klass, "경고")

    def test_unparsable_file_warns(self) -> None:
        self.write("{not json")
        row = self.row(self.run_check())
        self.assertEqual(row.klass, "경고")
        self.assertIn("unparsable", row.status)

    # -- file handling -----------------------------------------------------

    def test_unreadable_file_warns_only_where_hande_is_declared(self) -> None:
        self.write(body(self.now))
        denied = PermissionError(13, "Permission denied")
        with patch.object(ccc_doctor, "_read_regular_bounded", side_effect=denied):
            row = self.row(self.run_check())
            self.assertEqual(row.klass, "정상")
            self.assertIn("unreadable: permission", row.status)
            self.declare_hande()
            row = self.row(self.run_check())
            self.assertEqual(row.klass, "경고")
            self.assertIn("0640", row.action)

    def test_symlink_is_not_followed(self) -> None:
        target = self.write(body(self.now), self.base / "elsewhere.json")
        os.symlink(target, self.health)
        row = self.row(self.run_check())
        self.assertEqual(row.klass, "경고")
        self.assertIn("not a regular file", row.status)

    def test_each_found_file_gets_its_own_row(self) -> None:
        second = self.base / "termux" / "health.json"
        second.parent.mkdir()
        self.write(body(self.now))
        self.write(body(self.now, updated=int(self.now) - 4000), second)
        missing = self.base / "nowhere" / "health.json"
        rows = self.rows(self.run_check(paths=[self.health, missing, second]))
        self.assertEqual(len(rows), 2)
        self.assertEqual(sorted(r.klass for r in rows), ["경고", "정상"])
        self.assertTrue(all(str(self.base) in r.item for r in rows), [r.item for r in rows])

    def test_room_names_are_bounded_and_printable(self) -> None:
        rooms = [{"room": "x\x1b[31m" + "y" * 300, "halted": True, "halt_reason": "history_gap"}]
        self.write(body(self.now, rooms=rooms))
        row = self.row(self.run_check())
        self.assertNotIn("\x1b", row.status)
        self.assertLess(len(row.status), 400)

    def test_default_paths_cover_systemd_and_termux(self) -> None:
        doctor = Doctor(Path.cwd(), self.claude_dir, "settings")
        with patch.dict("os.environ", {"HOME": str(self.base)}, clear=True):
            paths = doctor._hande_bot_health_paths()
        self.assertIn(Path("/var/lib/native-relay-bot/rooms/health.json"), paths)
        self.assertIn(self.base / ".native-relay-bot" / "rooms" / "health.json", paths)


if __name__ == "__main__":
    unittest.main()
