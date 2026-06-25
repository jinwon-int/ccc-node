import importlib
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from unittest.mock import patch


class SessionManagerReplyModeTests(unittest.IsolatedAsyncioTestCase):
    def _load_session_manager_module(self, project_root: str, **extra_env):
        with patch.dict(
            os.environ,
            {
                "PROJECT_ROOT": project_root,
                "TELEGRAM_BOT_TOKEN": "123456:abc",
                **extra_env,
            },
            clear=True,
        ):
            for name in (
                "telegram_bot.utils.config",
                "telegram_bot.session.store",
                "telegram_bot.session.manager",
            ):
                sys.modules.pop(name, None)
            return importlib.import_module("telegram_bot.session.manager")

    async def test_get_session_sets_default_reply_mode(self):
        with TemporaryDirectory() as td:
            module = self._load_session_manager_module(td)
            manager = module.SessionManager()

            session = await manager.get_session(1001)
            self.assertEqual(session["reply_mode"], "text")

    async def test_set_reply_mode_normalizes_invalid_value(self):
        with TemporaryDirectory() as td:
            module = self._load_session_manager_module(td)
            manager = module.SessionManager()

            await manager.set_reply_mode(1001, "invalid-mode")
            mode = await manager.get_reply_mode(1001)
            self.assertEqual(mode, "text")

    async def test_set_reply_mode_persists_voice(self):
        with TemporaryDirectory() as td:
            module = self._load_session_manager_module(td)
            manager = module.SessionManager()

            await manager.set_reply_mode(1001, "voice")
            mode = await manager.get_reply_mode(1001)
            self.assertEqual(mode, "voice")

    async def test_get_session_returns_isolated_copy(self):
        # Mutating a returned session must NOT leak into the store before an
        # explicit update_session() — get() hands out a deep copy.
        with TemporaryDirectory() as td:
            module = self._load_session_manager_module(td)
            manager = module.SessionManager()

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
            module = self._load_session_manager_module(td)
            manager = module.SessionManager()

            payload = {"items": [1, 2]}
            await manager.update_session(1001, payload)
            payload["items"].append(999)  # mutate the nested list after commit

            fresh = await manager.get_session(1001)
            self.assertEqual(fresh["items"], [1, 2])

    async def test_should_start_new_session_is_false_without_previous_message(self):
        with TemporaryDirectory() as td:
            module = self._load_session_manager_module(td)
            manager = module.SessionManager()

            should_start = await manager.should_start_new_session(1001)
            self.assertFalse(should_start)

    async def test_should_start_new_session_uses_configured_threshold(self):
        with TemporaryDirectory() as td:
            module = self._load_session_manager_module(
                td, AUTO_NEW_SESSION_AFTER_HOURS="1"
            )
            manager = module.SessionManager()
            now = datetime(2026, 3, 20, 12, 0, tzinfo=timezone.utc)

            await manager.set_last_user_message_at(
                1001, now - timedelta(hours=1, minutes=1)
            )

            should_start = await manager.should_start_new_session(1001, now=now)
            self.assertTrue(should_start)

    async def test_should_start_new_session_can_be_disabled(self):
        with TemporaryDirectory() as td:
            module = self._load_session_manager_module(
                td, AUTO_NEW_SESSION_AFTER_HOURS="off"
            )
            manager = module.SessionManager()
            now = datetime(2026, 3, 20, 12, 0, tzinfo=timezone.utc)

            await manager.set_last_user_message_at(1001, now - timedelta(days=3))

            should_start = await manager.should_start_new_session(1001, now=now)
            self.assertFalse(should_start)


if __name__ == "__main__":
    unittest.main()
