import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from telegram_bot.session.manager import SessionManager
from telegram_bot.session.store import SessionStore


class SessionManagerReplyModeTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _make_manager(project_root: str, *, auto_new_session_after_hours=24.0):
        store = SessionStore(Path(project_root) / ".telegram_bot" / "sessions.json")
        manager = SessionManager(
            store=store,
            settings=SimpleNamespace(
                auto_new_session_after_hours=auto_new_session_after_hours
            ),
        )
        manager.initialize()
        return manager

    async def test_get_session_sets_default_reply_mode(self):
        with TemporaryDirectory() as td:
            manager = self._make_manager(td)

            session = await manager.get_session(1001)
            self.assertEqual(session["reply_mode"], "text")

    async def test_set_reply_mode_normalizes_invalid_value(self):
        with TemporaryDirectory() as td:
            manager = self._make_manager(td)

            await manager.set_reply_mode(1001, "invalid-mode")
            mode = await manager.get_reply_mode(1001)
            self.assertEqual(mode, "text")

    async def test_set_reply_mode_persists_voice(self):
        with TemporaryDirectory() as td:
            manager = self._make_manager(td)

            await manager.set_reply_mode(1001, "voice")
            mode = await manager.get_reply_mode(1001)
            self.assertEqual(mode, "voice")

    async def test_get_session_returns_isolated_copy(self):
        # Mutating a returned session must NOT leak into the store before an
        # explicit update_session() — get() hands out a deep copy.
        with TemporaryDirectory() as td:
            manager = self._make_manager(td)

            await manager.update_session(1001, {"model": "opus"})
            session = await manager.get_session(1001)
            session["model"] = "haiku"  # local mutation, no commit
            session["pending"] = [1, 2, 3]

            fresh = await manager.get_session(1001)
            self.assertEqual(fresh["model"], "opus")
            self.assertNotIn("pending", fresh)

    async def test_update_session_does_not_alias_caller_dict(self):
        # After update_session, mutating the dict the caller passed must not
        # change the stored state (store keeps a private copy).
        with TemporaryDirectory() as td:
            manager = self._make_manager(td)

            payload = {"items": [1, 2]}
            await manager.update_session(1001, payload)
            payload["items"].append(999)  # mutate the nested list after commit

            fresh = await manager.get_session(1001)
            self.assertEqual(fresh["items"], [1, 2])

    async def test_patch_session_updates_and_deletes_without_replacing_other_fields(self):
        with TemporaryDirectory() as td:
            manager = self._make_manager(td)
            await manager.update_session(
                1001,
                {
                    "reply_mode": "voice",
                    "pending_question": {"id": "q1"},
                    "newer_field": "preserve-me",
                },
            )

            await manager.patch_session(
                1001,
                updates={"session_id": "sid-2"},
                remove_fields={"pending_question"},
            )

            fresh = await manager.get_session(1001)
            self.assertEqual(fresh["session_id"], "sid-2")
            self.assertEqual(fresh["newer_field"], "preserve-me")
            self.assertNotIn("pending_question", fresh)

    async def test_patch_session_if_is_atomic_compare_and_consume(self):
        with TemporaryDirectory() as td:
            manager = self._make_manager(td)
            await manager.update_session(
                1001,
                {
                    "bash_approved_once": True,
                    "bash_approved_digest": "digest-1",
                    "newer_field": "preserve-me",
                },
            )

            first = await manager.patch_session_if(
                1001,
                expected={
                    "bash_approved_once": True,
                    "bash_approved_digest": "digest-1",
                },
                updates={"bash_approved_once": False},
                remove_fields={"bash_approved_digest"},
            )
            second = await manager.patch_session_if(
                1001,
                expected={
                    "bash_approved_once": True,
                    "bash_approved_digest": "digest-1",
                },
                updates={"bash_approved_once": False},
                remove_fields={"bash_approved_digest"},
            )

            self.assertTrue(first)
            self.assertFalse(second)
            fresh = await manager.get_session(1001)
            self.assertFalse(fresh["bash_approved_once"])
            self.assertNotIn("bash_approved_digest", fresh)
            self.assertEqual(fresh["newer_field"], "preserve-me")

    async def test_should_start_new_session_is_false_without_previous_message(self):
        with TemporaryDirectory() as td:
            manager = self._make_manager(td)

            should_start = await manager.should_start_new_session(1001)
            self.assertFalse(should_start)

    async def test_should_start_new_session_uses_configured_threshold(self):
        with TemporaryDirectory() as td:
            manager = self._make_manager(td, auto_new_session_after_hours=1.0)
            now = datetime(2026, 3, 20, 12, 0, tzinfo=timezone.utc)

            await manager.set_last_user_message_at(
                1001, now - timedelta(hours=1, minutes=1)
            )

            should_start = await manager.should_start_new_session(1001, now=now)
            self.assertTrue(should_start)

    async def test_should_start_new_session_can_be_disabled(self):
        with TemporaryDirectory() as td:
            manager = self._make_manager(td, auto_new_session_after_hours=None)
            now = datetime(2026, 3, 20, 12, 0, tzinfo=timezone.utc)

            await manager.set_last_user_message_at(1001, now - timedelta(days=3))

            should_start = await manager.should_start_new_session(1001, now=now)
            self.assertFalse(should_start)


if __name__ == "__main__":
    unittest.main()
