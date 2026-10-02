#!/usr/bin/env python3
"""Hermetic tests for the #2075 legacy group-session audit/migration tool.

Pins the detection rule (a room row sharing its session id with a DM/legacy
row or another room), that DM rows are never modified, that the dry-run is
read-only and body-free, and that ``--apply`` backs up first, refuses while
the bridge or a bound runner record is live, and is idempotent.
"""

from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ccc_session_scope_audit as mod  # noqa: E402

DM_SID = "dm-session-0001"
SECRET_MODEL = "model-marker-never-printed"
SECRET_LABEL = "resume label body never printed"


def key(suffix: str) -> str:
    return f"telegram_session:{suffix}"


def row(session_id, **extra):
    data = {"provider": "claude", "reply_mode": "text", "session_id": session_id}
    data.update(extra)
    return data


def flagged_keys(data):
    return [(r.key, r.reason) for r in mod.find_contaminated(data)[0]]


class DetectionRule(unittest.TestCase):
    def test_per_user_chat_room_seeded_from_dm_is_flagged(self):
        data = {key("7"): row(DM_SID), key("7:-100"): row(DM_SID)}
        self.assertEqual(flagged_keys(data), [("7:-100", mod.REASON_DM)])

    def test_shared_groups_room_seeded_from_any_dm_is_flagged(self):
        data = {key("8"): row(DM_SID), key("0:-100"): row(DM_SID)}
        self.assertEqual(flagged_keys(data), [("0:-100", mod.REASON_DM)])

    def test_both_scope_keys_of_one_room_are_flagged_when_dm_holds_the_id(self):
        data = {key("7"): row(DM_SID), key("7:-100"): row(DM_SID), key("0:-100"): row(DM_SID)}
        self.assertEqual(
            flagged_keys(data), [("0:-100", mod.REASON_DM), ("7:-100", mod.REASON_DM)]
        )

    def test_one_room_under_two_scope_keys_alone_is_not_flagged(self):
        data = {key("7:-100"): row("room"), key("0:-100"): row("room"), key("7"): row("dm")}
        self.assertEqual(flagged_keys(data), [])

    def test_two_rooms_sharing_an_id_are_both_flagged_cross_room(self):
        data = {key("7:-100"): row("x"), key("7:-200"): row("x"), key("7"): row("moved-on")}
        self.assertEqual(
            flagged_keys(data),
            [("7:-100", mod.REASON_CROSS_ROOM), ("7:-200", mod.REASON_CROSS_ROOM)],
        )

    def test_dm_rows_are_never_flagged(self):
        data = {key("7"): row("x"), key("8"): row("x")}
        self.assertEqual(flagged_keys(data), [])

    def test_shared_all_row_is_ignored_on_both_sides(self):
        data = {key("0:0"): row(DM_SID), key("7"): row(DM_SID), key("0:-100"): row("other")}
        self.assertEqual(flagged_keys(data), [])
        data = {key("0:0"): row("x"), key("0:-100"): row("x")}
        self.assertEqual(flagged_keys(data), [])

    def test_empty_or_cleared_ids_never_match(self):
        data = {key("7"): row(None), key("7:-100"): row(None), key("8"): row(""), key("8:-1"): row("")}
        self.assertEqual(flagged_keys(data), [])

    def test_unknown_key_shapes_fail_closed(self):
        for bad in ("telegram_session:7:1:2", "telegram_session:x", "other:7"):
            with self.subTest(bad=bad), self.assertRaises(mod.AuditError):
                mod.find_contaminated({bad: row("x")})


class StoreFixture(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.data_dir = Path(self._tmp.name) / ".telegram_bot"
        self.data_dir.mkdir(mode=0o700)
        self.store = self.data_dir / "sessions.json"
        self.data = {
            key("7"): row(DM_SID, model=SECRET_MODEL),
            key("7:-100"): row(
                DM_SID, model=SECRET_MODEL, effort="high", resume_list=[["sid", SECRET_LABEL, "claude"]]
            ),
            key("9:-300"): row("room-own-session"),
        }
        self.write_store(self.data)

    def tearDown(self):
        self._tmp.cleanup()

    def write_store(self, data):
        payload = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode()
        fd = os.open(self.store, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        return payload

    def run_main(self, *extra):
        out = io.StringIO()
        code = mod.main(["--store", str(self.store), *extra], out=out)
        return code, out.getvalue()

    def backups(self):
        return sorted(self.data_dir.glob("sessions.json.bak-2075-*"))

    def write_json(self, relative, value):
        path = self.data_dir / relative
        path.parent.mkdir(mode=0o700, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle)


class DryRun(StoreFixture):
    def test_dry_run_reports_keys_and_counts_only_and_writes_nothing(self):
        before = self.store.read_bytes()
        code, out = self.run_main()
        self.assertEqual(code, mod.RC_FLAGGED)
        self.assertIn("rows=3 room_rows=2 flagged=1", out)
        self.assertIn("flagged key=7:-100 reason=dm-session", out)
        self.assertIn("mode=dry-run", out)
        for secret in (DM_SID, SECRET_MODEL, SECRET_LABEL, "room-own-session", "high"):
            self.assertNotIn(secret, out)
        self.assertEqual(self.store.read_bytes(), before)
        self.assertEqual(self.backups(), [])
        self.assertFalse((self.data_dir / "sessions.json.bak").exists())

    def test_clean_store_exits_zero(self):
        self.write_store({key("7"): row("a"), key("7:-100"): row("b")})
        code, out = self.run_main()
        self.assertEqual(code, mod.RC_OK)
        self.assertIn("flagged=0", out)

    def test_unreadable_store_is_an_error_not_a_clean_result(self):
        self.store.write_text("{not json", encoding="utf-8")
        code, out = self.run_main()
        self.assertEqual(code, mod.RC_ERROR)
        self.assertIn("error=not-json", out)

    def test_group_writable_store_is_refused(self):
        os.chmod(self.store, 0o620)
        code, out = self.run_main()
        self.assertEqual(code, mod.RC_ERROR)
        self.assertIn("error=unreadable", out)


class Apply(StoreFixture):
    def test_apply_backs_up_then_clears_only_the_room_row(self):
        original = self.store.read_bytes()
        code, out = self.run_main("--apply")
        self.assertEqual(code, mod.RC_OK, out)
        self.assertIn("applied cleared=1", out)
        self.assertNotIn(DM_SID, out)

        backups = self.backups()
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)
        self.assertEqual(stat.S_IMODE(backups[0].stat().st_mode), 0o600)
        self.assertEqual((self.data_dir / "sessions.json.bak").read_bytes(), original)
        self.assertEqual(stat.S_IMODE(self.store.stat().st_mode), 0o600)

        after = json.loads(self.store.read_text(encoding="utf-8"))
        self.assertEqual(after[key("7")], self.data[key("7")])
        self.assertEqual(after[key("9:-300")], self.data[key("9:-300")])
        room = after[key("7:-100")]
        self.assertIsNone(room["session_id"])
        self.assertIs(room["new_session"], True)
        for field in ("model", "effort", "resume_list", "provider", "reply_mode"):
            self.assertEqual(room[field], self.data[key("7:-100")][field])
        # The first-use seed copies a legacy row only into a row that holds
        # nothing but reply_mode/provider (bridge/core/bot.py); the cleared row
        # must never look like that, or the next turn would re-seed the DM id.
        self.assertFalse(set(room).issubset({"reply_mode", "provider"}))

    def test_apply_is_idempotent(self):
        self.assertEqual(self.run_main("--apply")[0], mod.RC_OK)
        migrated = self.store.read_bytes()
        code, out = self.run_main("--apply")
        self.assertEqual(code, mod.RC_OK)
        self.assertIn("flagged=0", out)
        self.assertEqual(self.store.read_bytes(), migrated)
        self.assertEqual(len(self.backups()), 1)

    def test_apply_refuses_while_the_bridge_runs(self):
        bridge = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)", "telegram_bot"]
        )
        try:
            (self.data_dir / "bot.pid").write_text(f"{bridge.pid}\n", encoding="utf-8")
            before = self.store.read_bytes()
            code, out = self.run_main("--apply")
        finally:
            bridge.kill()
            bridge.wait()
        self.assertEqual(code, mod.RC_REFUSED)
        self.assertIn("bridge=running", out)
        self.assertIn("apply refused: bridge-running", out)
        self.assertEqual(self.store.read_bytes(), before)
        self.assertEqual(self.backups(), [])

    def test_stale_pid_of_another_process_does_not_block(self):
        (self.data_dir / "bot.pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
        self.assertFalse(mod.bridge_running(self.data_dir))
        (self.data_dir / "bot.pid").write_text("999999999\n", encoding="utf-8")
        self.assertFalse(mod.bridge_running(self.data_dir))

    def test_apply_refuses_while_a_live_wait_is_bound_to_the_flagged_id(self):
        self.write_json(
            "external-wait/waits.json",
            {
                "w1": {"session_id": DM_SID, "chat_id": -100, "state": "monitoring"},
                "w2": {
                    "session_id": DM_SID, "chat_id": -100, "state": "success", "wake": {"state": "done"}
                },
                # The owner's own DM wait reads the DM row; it never blocks.
                "w3": {"session_id": DM_SID, "chat_id": 7, "state": "monitoring"},
            },
        )
        code, out = self.run_main("--apply")
        self.assertEqual(code, mod.RC_REFUSED)
        self.assertIn("pending_runner_records=1", out)
        self.assertIn("apply refused: pending-runner-records", out)
        self.assertEqual(self.backups(), [])

    def test_pending_wake_and_pending_continuation_count(self):
        self.write_json(
            "external-wait/waits.json",
            {
                "w1": {
                    "session_id": DM_SID, "chat_id": -100, "state": "success", "wake": {"state": "pending"}
                }
            },
        )
        self.write_json(
            "continuation/queue.json",
            {
                "records": {
                    "c1": {"session_id": DM_SID, "chat_id": -100, "state": "pending"},
                    "c2": {"session_id": DM_SID, "chat_id": -100, "state": "done"},
                    "c3": {"session_id": "room-own-session", "chat_id": -300, "state": "pending"},
                    "c4": {"session_id": DM_SID, "chat_id": 7, "state": "running"},
                },
                "counters": {},
            },
        )
        audit = mod.audit_store(self.store)
        self.assertEqual(audit.pending_runner_records, 2)

    def test_terminal_room_and_live_dm_runner_records_do_not_block(self):
        self.write_json(
            "continuation/queue.json",
            {
                "records": {
                    "c1": {"session_id": DM_SID, "chat_id": -100, "state": "done"},
                    "c2": {"session_id": DM_SID, "chat_id": 7, "state": "pending"},
                },
                "counters": {},
            },
        )
        self.assertEqual(self.run_main("--apply")[0], mod.RC_OK)

    def test_unreadable_runner_state_fails_closed(self):
        path = self.data_dir / "external-wait" / "waits.json"
        path.parent.mkdir(mode=0o700)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
        os.write(fd, b"[broken")
        os.close(fd)
        code, out = self.run_main("--apply")
        self.assertEqual(code, mod.RC_REFUSED)
        self.assertIn("pending_runner_records=unknown", out)
        self.assertIn("runner-state-unreadable", out)

    def test_store_changed_after_audit_is_refused(self):
        audit = mod.audit_store(self.store)
        self.write_store({**self.data, key("5"): row("new")})
        with self.assertRaises(mod.AuditError) as caught:
            mod.apply_audit(audit)
        self.assertEqual(str(caught.exception), "store-changed")
        self.assertEqual(self.backups(), [])


class DefaultStores(unittest.TestCase):
    def test_defaults_are_existing_telegram_and_matrix_stores(self):
        with TemporaryDirectory() as temp:
            home = Path(temp)
            self.assertEqual(mod.default_stores({"HOME": temp}), [])
            for name in (".telegram_bot", ".ccc-matrix"):
                (home / name).mkdir()
                (home / name / "sessions.json").write_text("{}", encoding="utf-8")
            self.assertEqual(
                mod.default_stores({"HOME": temp}),
                [home / ".telegram_bot" / "sessions.json", home / ".ccc-matrix" / "sessions.json"],
            )
            custom = home / "custom"
            custom.mkdir()
            (custom / "sessions.json").write_text("{}", encoding="utf-8")
            stores = mod.default_stores({"HOME": temp, "BOT_DATA_DIR": str(custom)})
            self.assertEqual(stores[0], custom / "sessions.json")


if __name__ == "__main__":
    unittest.main()
