"""Behavior tests for BotAccessMixin deny/error/empty/no-match paths (#348).

These drive the real mixin against a real SessionManager/SessionStore in a
temp directory — no source-string assertions — covering: allowlist denials
per update type, stale-message drops, the AskUserQuestion downgrade, the
fail-closed Bash policies, the per-call Bash approval round-trip (including
digest mismatch), and the outside-path approval round-trip.
"""

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny

from telegram_bot.core.bot_access import BotAccessMixin
from telegram_bot.session.manager import SessionManager
from telegram_bot.session.store import SessionStore


class _Recorder:
    def __init__(self):
        self.calls = []

    async def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))


class AccessHarness(BotAccessMixin):
    """Real mixin over a real session store; only Telegram objects are fake."""

    _ALLOW_OUTSIDE_ONCE_TOKEN = "ALLOW_OUTSIDE_ONCE"
    _DENY_OUTSIDE_TOKEN = "DENY_OUTSIDE"

    def __init__(self, tmpdir: str, *, allowed=(1,), bash_policy="approve-each"):
        self._config = SimpleNamespace(
            allowed_user_ids=list(allowed),
            project_root=str(tmpdir),
            execution_profile="strict-project",
            bash_policy=bash_policy,
            require_allowlist=True,
        )
        store = SessionStore(Path(tmpdir) / "sessions.json")
        store.initialize()
        self._session_manager = SessionManager(
            store, SimpleNamespace(agent_provider="claude")
        )

    @staticmethod
    def _conversation_key(user_id: int, chat_id: Optional[int] = None) -> Any:
        if chat_id is None or chat_id == user_id:
            return user_id
        return f"{user_id}:{chat_id}"


def _update(
    *,
    user_id: Optional[int] = 1,
    text_message: bool = True,
    voice: bool = False,
    callback: bool = False,
    age_seconds: float = 0.0,
):
    message = None
    callback_query = None
    reply = _Recorder()
    answer = _Recorder()
    if text_message:
        message = SimpleNamespace(
            date=datetime.now(timezone.utc) - timedelta(seconds=age_seconds),
            voice=object() if voice else None,
            reply_text=reply,
        )
    if callback:
        callback_query = SimpleNamespace(message=None, answer=answer)
    user = SimpleNamespace(id=user_id) if user_id is not None else None
    update = SimpleNamespace(
        message=message, callback_query=callback_query, effective_user=user
    )
    return update, reply, answer


class CheckAccessTests(unittest.TestCase):
    def _harness(self, **kwargs) -> AccessHarness:
        tmpdir = tempfile.mkdtemp()
        return AccessHarness(tmpdir, **kwargs)

    def test_empty_allowlist_allows_every_user(self):
        bot = self._harness(allowed=())
        self.assertTrue(bot._check_user_access(424242))

    def test_allowlist_member_allowed_and_stranger_denied(self):
        bot = self._harness(allowed=(1, 2))
        self.assertTrue(bot._check_user_access(2))
        self.assertFalse(bot._check_user_access(99))

    def test_stale_message_is_dropped_without_reply(self):
        bot = self._harness()
        update, reply, _ = _update(user_id=1, age_seconds=21 * 60)
        self.assertFalse(asyncio.run(bot._check_access(update)))
        self.assertEqual(reply.calls, [])

    def test_update_without_user_is_denied(self):
        bot = self._harness()
        update, reply, _ = _update(user_id=None)
        self.assertFalse(asyncio.run(bot._check_access(update)))
        self.assertEqual(reply.calls, [])

    def test_denied_text_message_gets_permission_reply(self):
        bot = self._harness(allowed=(1,))
        update, reply, _ = _update(user_id=99)
        self.assertFalse(asyncio.run(bot._check_access(update)))
        self.assertEqual(len(reply.calls), 1)
        self.assertIn("permission", reply.calls[0][0][0])

    def test_denied_voice_message_gets_voice_specific_reply(self):
        bot = self._harness(allowed=(1,))
        update, reply, _ = _update(user_id=99, voice=True)
        self.assertFalse(asyncio.run(bot._check_access(update)))
        self.assertIn("voice", reply.calls[0][0][0])

    def test_denied_callback_query_gets_alert_answer(self):
        bot = self._harness(allowed=(1,))
        update, _, answer = _update(user_id=99, text_message=False, callback=True)
        self.assertFalse(asyncio.run(bot._check_access(update)))
        self.assertEqual(len(answer.calls), 1)
        self.assertTrue(answer.calls[0][1].get("show_alert"))

    def test_fresh_message_from_allowed_user_passes(self):
        bot = self._harness(allowed=(1,))
        update, reply, _ = _update(user_id=1)
        self.assertTrue(asyncio.run(bot._check_access(update)))
        self.assertEqual(reply.calls, [])


class PermissionCallbackTests(unittest.TestCase):
    def _harness(self, **kwargs) -> AccessHarness:
        tmpdir = tempfile.mkdtemp()
        return AccessHarness(tmpdir, **kwargs)

    def test_ask_user_question_is_downgraded_to_numbered_options(self):
        bot = self._harness()
        result = asyncio.run(bot._permission_callback(10, 1, "AskUserQuestion", {}))
        self.assertIsInstance(result, PermissionResultDeny)
        self.assertIn("numbered options", result.message.lower().replace("-", " "))

    def test_bash_auto_approve_allows(self):
        bot = self._harness(bash_policy="auto-approve")
        result = asyncio.run(
            bot._permission_callback(10, 1, "Bash", {"command": "ls"})
        )
        self.assertIsInstance(result, PermissionResultAllow)

    def test_bash_disabled_policy_fails_closed(self):
        bot = self._harness(bash_policy="disabled")
        result = asyncio.run(
            bot._permission_callback(10, 1, "Bash", {"command": "ls"})
        )
        self.assertIsInstance(result, PermissionResultDeny)
        self.assertIn("disabled", result.message)

    def test_bash_approve_each_denies_first_and_records_pending_approval(self):
        bot = self._harness(bash_policy="approve-each")
        tool_input = {"command": "cat /etc/hosts"}

        result = asyncio.run(bot._permission_callback(10, 1, "Bash", tool_input))
        self.assertIsInstance(result, PermissionResultDeny)

        session = asyncio.run(bot._session_manager.get_session("1:10"))
        self.assertEqual(session.get("pending_approval_kind"), "bash")
        self.assertTrue(session.get("pending_outside_paths"))
        self.assertEqual(
            session.get("pending_approval_digest"),
            bot._approval_digest("Bash", tool_input),
        )

    def test_bash_approval_round_trip_allows_exactly_once(self):
        bot = self._harness(bash_policy="approve-each")
        tool_input = {"command": "cat /etc/hosts"}

        async def scenario():
            first = await bot._permission_callback(10, 1, "Bash", tool_input)
            await bot._maybe_capture_outside_approval(1, "ALLOW_OUTSIDE_ONCE", 10)
            second = await bot._permission_callback(10, 1, "Bash", tool_input)
            third = await bot._permission_callback(10, 1, "Bash", tool_input)
            return first, second, third

        first, second, third = asyncio.run(scenario())
        self.assertIsInstance(first, PermissionResultDeny)
        self.assertIsInstance(second, PermissionResultAllow)
        # The approval is single-use: an identical follow-up call re-prompts.
        self.assertIsInstance(third, PermissionResultDeny)

    def test_bash_approval_digest_mismatch_denies_and_clears_flag(self):
        bot = self._harness(bash_policy="approve-each")

        async def scenario():
            await bot._permission_callback(10, 1, "Bash", {"command": "ls"})
            await bot._maybe_capture_outside_approval(1, "ALLOW_OUTSIDE_ONCE", 10)
            # A different command must not consume the approval for "ls".
            mismatched = await bot._permission_callback(
                10, 1, "Bash", {"command": "rm -rf /"}
            )
            session = await bot._session_manager.get_session("1:10")
            return mismatched, session

        mismatched, session = asyncio.run(scenario())
        self.assertIsInstance(mismatched, PermissionResultDeny)
        self.assertFalse(session.get("bash_approved_once", False))
        self.assertNotIn("bash_approved_digest", session)

    def test_bash_denial_reply_keeps_bash_denied(self):
        bot = self._harness(bash_policy="approve-each")

        async def scenario():
            await bot._permission_callback(10, 1, "Bash", {"command": "ls"})
            await bot._maybe_capture_outside_approval(1, "DENY_OUTSIDE", 10)
            return await bot._permission_callback(10, 1, "Bash", {"command": "ls"})

        self.assertIsInstance(asyncio.run(scenario()), PermissionResultDeny)

    def test_outside_path_denies_first_then_allows_after_approval(self):
        bot = self._harness()

        async def scenario():
            first = await bot._permission_callback(
                10, 1, "Read", {"file_path": "/etc/passwd"}
            )
            session = await bot._session_manager.get_session("1:10")
            await bot._maybe_capture_outside_approval(1, "ALLOW_OUTSIDE_ONCE", 10)
            second = await bot._permission_callback(
                10, 1, "Read", {"file_path": "/etc/passwd"}
            )
            return first, session, second

        first, session, second = asyncio.run(scenario())
        self.assertIsInstance(first, PermissionResultDeny)
        self.assertIn("/etc/passwd", first.message)
        self.assertEqual(session.get("pending_approval_kind"), "outside-path")
        self.assertIsInstance(second, PermissionResultAllow)

    def test_path_inside_project_root_is_allowed_without_prompt(self):
        tmpdir = tempfile.mkdtemp()
        bot = AccessHarness(tmpdir)
        inside = str(Path(tmpdir) / "notes.txt")
        result = asyncio.run(
            bot._permission_callback(10, 1, "Read", {"file_path": inside})
        )
        self.assertIsInstance(result, PermissionResultAllow)

    def test_unrelated_reply_leaves_pending_approval_untouched(self):
        bot = self._harness()

        async def scenario():
            await bot._permission_callback(10, 1, "Read", {"file_path": "/etc/passwd"})
            await bot._maybe_capture_outside_approval(1, "just chatting", 10)
            return await bot._session_manager.get_session("1:10")

        session = asyncio.run(scenario())
        self.assertTrue(session.get("pending_outside_paths"))
        self.assertFalse(session.get("outside_path_approved_once", False))

    def test_capture_is_noop_without_pending_request(self):
        bot = self._harness()

        async def scenario():
            await bot._maybe_capture_outside_approval(1, "ALLOW_OUTSIDE_ONCE", 10)
            return await bot._session_manager.get_session("1:10")

        session = asyncio.run(scenario())
        self.assertFalse(session.get("outside_path_approved_once", False))
        self.assertFalse(session.get("bash_approved_once", False))


if __name__ == "__main__":
    unittest.main()
