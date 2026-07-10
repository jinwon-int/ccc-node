"""Bash auto-approval, per-call approval, and fail-closed regression tests."""

import asyncio
import os
import unittest
from unittest.mock import patch

from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny

from telegram_bot.core import bot_access
from telegram_bot.core.bot_access import BotAccessMixin
from telegram_bot.core import project_chat, tool_policy


class _FakeSDKClient:
    last_options = None

    def __init__(self, options):
        type(self).last_options = options

    async def connect(self):
        return None


class SDKOptionWiringTest(unittest.TestCase):
    def test_project_chat_defaults_to_bare_bash_auto_approval(self):
        def close_task(coro):
            coro.close()
            return object()

        with patch.object(project_chat, "ClaudeSDKClient", _FakeSDKClient), patch.object(
            project_chat.asyncio, "create_task", side_effect=close_task
        ):
            handler = project_chat.ProjectChatHandler()
            asyncio.run(handler._create_user_stream(10, None))

        options = _FakeSDKClient.last_options
        self.assertIsNotNone(options)
        self.assertIn("Bash", options.allowed_tools)
        self.assertNotIn("Bash", options.disallowed_tools)
        self.assertEqual(options.hooks, {})


class ToolPolicyTest(unittest.TestCase):
    def test_default_auto_approves_bash(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(tool_policy.resolve_bash_policy(), "auto-approve")
            self.assertIn("Bash", tool_policy.allowed_tools())
            self.assertNotIn("Bash", tool_policy.disallowed_tools())
            self.assertIn("AskUserQuestion", tool_policy.disallowed_tools())
            self.assertEqual(tool_policy.bash_permission_hooks(), {})

    def test_auto_approve_normalizes_underscore_form(self):
        self.assertEqual(
            tool_policy.resolve_bash_policy("auto_approve"), "auto-approve"
        )
        self.assertIn("Bash", tool_policy.allowed_tools("auto_approve"))

    def test_explicit_disabled_policy_hard_denies_bash(self):
        with patch.dict(
            os.environ, {"CCC_BRIDGE_BASH_POLICY": "disabled"}, clear=True
        ):
            self.assertEqual(tool_policy.resolve_bash_policy(), "disabled")
            self.assertNotIn("Bash", tool_policy.allowed_tools())
            self.assertIn("Bash", tool_policy.disallowed_tools())

    def test_invalid_value_fails_closed(self):
        with patch.dict(os.environ, {"CCC_BRIDGE_BASH_POLICY": "allow"}, clear=True):
            self.assertEqual(tool_policy.resolve_bash_policy(), "disabled")
            self.assertNotIn("Bash", tool_policy.allowed_tools())

    def test_approve_each_exposes_bash_without_auto_approving_it(self):
        tools = tool_policy.allowed_tools("approve-each")
        self.assertNotIn("Bash", tools)
        self.assertIn("Read", tools)
        self.assertIn("Write", tools)
        self.assertNotIn("Bash", tool_policy.disallowed_tools("approve-each"))
        self.assertIn("AskUserQuestion", tool_policy.disallowed_tools("approve-each"))

    def test_approve_each_registers_pretool_ask_hook_for_bash(self):
        hooks = tool_policy.bash_permission_hooks("approve-each")
        self.assertEqual(set(hooks), {"PreToolUse"})
        self.assertEqual(len(hooks["PreToolUse"]), 1)
        matcher = hooks["PreToolUse"][0]
        self.assertEqual(matcher.matcher, "Bash")
        self.assertEqual(len(matcher.hooks), 1)

        result = asyncio.run(
            matcher.hooks[0](
                {
                    "hook_event_name": "PreToolUse",
                    "tool_name": "Bash",
                    "tool_input": {"command": "pwd"},
                },
                "tool-use-id",
                None,
            )
        )
        specific = result["hookSpecificOutput"]
        self.assertEqual(specific["hookEventName"], "PreToolUse")
        self.assertEqual(specific["permissionDecision"], "ask")

    def test_disabled_policy_does_not_register_bash_hook(self):
        self.assertEqual(tool_policy.bash_permission_hooks("disabled"), {})

    def test_bash_without_active_callback_is_policy_specific(self):
        self.assertTrue(
            tool_policy.missing_callback_requires_denial("Bash", "approve-each")
        )
        self.assertFalse(
            tool_policy.missing_callback_requires_denial("Bash", "auto-approve")
        )
        self.assertTrue(tool_policy.missing_callback_requires_denial("Bash", "disabled"))
        self.assertFalse(tool_policy.missing_callback_requires_denial("Read", "disabled"))


class _MemorySessionManager:
    def __init__(self):
        self.sessions = {}

    async def get_session(self, key):
        return self.sessions.setdefault(key, {})

    async def update_session(self, key, session):
        self.sessions[key] = dict(session)


class _AccessSubject(BotAccessMixin):
    _ALLOW_OUTSIDE_ONCE_TOKEN = "ALLOW_OUTSIDE_ONCE"
    _DENY_OUTSIDE_TOKEN = "DENY_OUTSIDE"

    def __init__(self, bash_policy):
        self._test_bash_policy = bash_policy

    @staticmethod
    def _conversation_key(user_id, chat_id=None):
        return f"{user_id}:{chat_id}" if chat_id is not None else str(user_id)

    def _bash_policy(self):
        return self._test_bash_policy


class BashPermissionFlowTest(unittest.TestCase):
    def setUp(self):
        self.sessions = _MemorySessionManager()
        self.session_patch = patch.object(bot_access, "session_manager", self.sessions)
        self.session_patch.start()
        self.addCleanup(self.session_patch.stop)

    def call(self, subject, command):
        return asyncio.run(
            subject._permission_callback(10, 20, "Bash", {"command": command})
        )

    def test_disabled_policy_denies_even_if_callback_is_called(self):
        result = self.call(_AccessSubject("disabled"), "pwd")
        self.assertIsInstance(result, PermissionResultDeny)
        self.assertIn("disabled", result.message.lower())

    def test_auto_approve_policy_allows_without_one_time_token(self):
        result = self.call(_AccessSubject("auto-approve"), "pwd")
        self.assertIsInstance(result, PermissionResultAllow)

    def test_every_per_call_bash_form_requires_approval(self):
        subject = _AccessSubject("approve-each")
        commands = [
            "pwd",
            'P=/etc/passwd; cat "$P"',
            'python -c \'open("/etc/passwd").read()\'',
            "cd .. && pwd",
            "cat $(printf /etc/passwd)",
        ]
        for command in commands:
            with self.subTest(command=command):
                result = self.call(subject, command)
                self.assertIsInstance(result, PermissionResultDeny)
                self.assertIn("Bash", result.message)

    def test_one_time_approval_is_consumed_and_logged(self):
        subject = _AccessSubject("approve-each")
        key = subject._conversation_key(20, 10)
        digest = subject._approval_digest("Bash", {"command": "pwd"})
        self.sessions.sessions[key] = {
            "bash_approved_once": True,
            "bash_approved_digest": digest,
            "pending_approval_kind": "bash",
        }
        with self.assertLogs("telegram_bot.core.bot_access", level="INFO") as logs:
            result = self.call(subject, "pwd")
        self.assertIsInstance(result, PermissionResultAllow)
        self.assertFalse(self.sessions.sessions[key]["bash_approved_once"])
        self.assertNotIn("bash_approved_digest", self.sessions.sessions[key])
        self.assertTrue(any("bash_approval_consumed" in line for line in logs.output))

    def test_one_time_approval_cannot_authorize_a_different_command(self):
        subject = _AccessSubject("approve-each")
        key = subject._conversation_key(20, 10)
        self.sessions.sessions[key] = {
            "bash_approved_once": True,
            "bash_approved_digest": subject._approval_digest(
                "Bash", {"command": "pwd"}
            ),
            "pending_approval_kind": "bash",
        }
        with self.assertLogs("telegram_bot.core.bot_access", level="WARNING") as logs:
            result = self.call(subject, "cat /etc/passwd")
        self.assertIsInstance(result, PermissionResultDeny)
        self.assertFalse(self.sessions.sessions[key]["bash_approved_once"])
        self.assertNotIn("bash_approved_digest", self.sessions.sessions[key])
        self.assertTrue(
            any("bash_approval_digest_mismatch" in line for line in logs.output)
        )

    def test_outside_path_approval_cannot_authorize_bash(self):
        subject = _AccessSubject("approve-each")
        key = subject._conversation_key(20, 10)
        self.sessions.sessions[key] = {
            "outside_path_approved_once": True,
            "pending_approval_kind": "outside-path",
        }
        result = self.call(subject, "pwd")
        self.assertIsInstance(result, PermissionResultDeny)
        self.assertTrue(self.sessions.sessions[key]["outside_path_approved_once"])

    def test_allow_reply_creates_bash_specific_token(self):
        subject = _AccessSubject("approve-each")
        key = subject._conversation_key(20, 10)
        self.sessions.sessions[key] = {
            "pending_outside_paths": ["Bash command requires per-call approval"],
            "pending_approval_kind": "bash",
            "pending_approval_digest": subject._approval_digest(
                "Bash", {"command": "pwd"}
            ),
        }
        asyncio.run(subject._maybe_capture_outside_approval(20, "ALLOW_OUTSIDE_ONCE", 10))
        self.assertTrue(self.sessions.sessions[key]["bash_approved_once"])
        self.assertTrue(self.sessions.sessions[key]["bash_approved_digest"])
        self.assertFalse(self.sessions.sessions[key].get("outside_path_approved_once", False))

    def test_button_reply_creates_token_only_in_the_matching_chat(self):
        subject = _AccessSubject("approve-each")
        key = subject._conversation_key(20, 10)
        self.sessions.sessions[key] = {
            "pending_outside_paths": ["Bash command requires per-call approval"],
            "pending_approval_kind": "bash",
            "pending_approval_digest": subject._approval_digest(
                "Bash", {"command": "pwd"}
            ),
        }
        choice = "1. ALLOW_OUTSIDE_ONCE (Allow this Bash call once)"
        asyncio.run(subject._maybe_capture_outside_approval(20, choice, 11))
        self.assertFalse(self.sessions.sessions[key].get("bash_approved_once", False))
        asyncio.run(subject._maybe_capture_outside_approval(20, choice, 10))
        self.assertTrue(self.sessions.sessions[key]["bash_approved_once"])

    def test_approval_token_must_be_an_explicit_reply(self):
        subject = _AccessSubject("approve-each")
        key = subject._conversation_key(20, 10)
        self.sessions.sessions[key] = {
            "pending_outside_paths": ["Bash command requires per-call approval"],
            "pending_approval_kind": "bash",
            "pending_approval_digest": subject._approval_digest(
                "Bash", {"command": "pwd"}
            ),
        }
        for text in (
            "do not use ALLOW_OUTSIDE_ONCE here",
            "1",
            "yes",
            "allow",
        ):
            with self.subTest(text=text):
                asyncio.run(subject._maybe_capture_outside_approval(20, text, 10))
                self.assertFalse(
                    self.sessions.sessions[key].get("bash_approved_once", False)
                )
                self.assertIn("pending_outside_paths", self.sessions.sessions[key])

    def test_denial_reply_is_logged_and_clears_pending_digest(self):
        subject = _AccessSubject("approve-each")
        key = subject._conversation_key(20, 10)
        self.sessions.sessions[key] = {
            "pending_outside_paths": ["Bash command requires per-call approval"],
            "pending_approval_kind": "bash",
            "pending_approval_digest": subject._approval_digest(
                "Bash", {"command": "pwd"}
            ),
        }
        with self.assertLogs("telegram_bot.core.bot_access", level="INFO") as logs:
            asyncio.run(
                subject._maybe_capture_outside_approval(
                    20, "2. DENY_OUTSIDE (Deny)", 10
                )
            )
        self.assertFalse(self.sessions.sessions[key].get("bash_approved_once", False))
        self.assertNotIn("pending_approval_digest", self.sessions.sessions[key])
        self.assertTrue(
            any("bash_approval_reply_denied" in line for line in logs.output)
        )

    def test_denial_records_pending_state_and_telemetry(self):
        subject = _AccessSubject("approve-each")
        key = subject._conversation_key(20, 10)
        with self.assertLogs("telegram_bot.core.bot_access", level="WARNING") as logs:
            result = self.call(subject, "pwd")
        self.assertIsInstance(result, PermissionResultDeny)
        self.assertTrue(self.sessions.sessions[key]["pending_outside_paths"])
        self.assertEqual(self.sessions.sessions[key]["pending_approval_kind"], "bash")
        self.assertTrue(any("bash_approval_required" in line for line in logs.output))


if __name__ == "__main__":
    unittest.main()
