"""Bash auto-approval, per-call approval, and fail-closed regression tests."""

import asyncio
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny

from telegram_bot.core.bot_access import BotAccessMixin
from telegram_bot.core import project_chat, tool_policy


class ExecutionProfileTest(unittest.TestCase):
    def test_default_profile_preserves_strict_project_sandbox(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                tool_policy.resolve_execution_profile(
                    allowed_user_ids=[42], require_allowlist=True
                ),
                "strict-project",
            )

    def test_owner_operator_requires_exactly_one_allowlisted_owner(self):
        accepted = tool_policy.resolve_execution_profile(
            "owner_operator", allowed_user_ids=[42], require_allowlist=True
        )
        self.assertEqual(accepted, "owner-operator")
        self.assertEqual(
            tool_policy.resolve_execution_profile(
                " owner_operator ", allowed_user_ids=[42, 42], require_allowlist=True
            ),
            "owner-operator",
        )

        unsafe_inputs = (
            ([], True),
            ([42, 43], True),
            ([42], False),
        )
        for allowed_user_ids, require_allowlist in unsafe_inputs:
            with self.subTest(
                allowed_user_ids=allowed_user_ids,
                require_allowlist=require_allowlist,
            ):
                self.assertEqual(
                    tool_policy.resolve_execution_profile(
                        "owner-operator",
                        allowed_user_ids=allowed_user_ids,
                        require_allowlist=require_allowlist,
                    ),
                    "disabled",
                )

    def test_unknown_profile_fails_closed_to_disabled(self):
        self.assertEqual(
            tool_policy.resolve_execution_profile(
                "host-unrestricted",
                allowed_user_ids=[42],
                require_allowlist=True,
            ),
            "disabled",
        )

    def test_disabled_profile_overrides_bash_auto_approval(self):
        self.assertEqual(
            tool_policy.effective_bash_policy("auto-approve", "disabled"),
            "disabled",
        )
        self.assertEqual(
            tool_policy.effective_bash_policy("approve-each", "strict-project"),
            "approve-each",
        )


class StrictBashSandboxTest(unittest.TestCase):
    def test_strict_sandbox_fails_closed_without_escape_hatches(self):
        root = Path("/srv/example-project")
        sandbox = tool_policy.strict_bash_sandbox_settings(root)

        self.assertTrue(sandbox["enabled"])
        self.assertTrue(sandbox["autoAllowBashIfSandboxed"])
        self.assertTrue(sandbox["failIfUnavailable"])
        self.assertFalse(sandbox["allowUnsandboxedCommands"])
        self.assertEqual(sandbox["excludedCommands"], [])
        self.assertFalse(sandbox["enableWeakerNestedSandbox"])
        self.assertEqual(sandbox["ignoreViolations"], {"file": [], "network": []})

    def test_strict_sandbox_denies_host_reads_and_reallows_only_project_and_runtime(self):
        root = Path("/srv/example-project")
        sandbox = tool_policy.strict_bash_sandbox_settings(root)
        filesystem = sandbox["filesystem"]

        self.assertEqual(filesystem["denyRead"], ["/"])
        self.assertIn(str(root), filesystem["allowRead"])
        self.assertNotIn("/etc", filesystem["allowRead"])
        self.assertNotIn("/proc", filesystem["allowRead"])
        self.assertNotIn("/dev", filesystem["allowRead"])
        self.assertNotIn(str(Path.home()), filesystem["allowRead"])
        self.assertEqual(filesystem["allowWrite"], [str(root)])
        self.assertEqual(filesystem["denyWrite"], [])

    def test_sandbox_reallows_only_sdk_and_resolved_cli_bootstrap_roots(self):
        with (
            patch.object(
                tool_policy.claude_agent_sdk,
                "__file__",
                "/opt/ccc-bridge/venv/claude_agent_sdk/__init__.py",
            ),
            patch.object(tool_policy.shutil, "which", return_value="/opt/claude/bin/claude"),
        ):
            sandbox = tool_policy.strict_bash_sandbox_settings(Path("/srv/project"))

        allow_read = sandbox["filesystem"]["allowRead"]
        self.assertIn("/opt/ccc-bridge/venv/claude_agent_sdk", allow_read)
        self.assertIn("/opt/claude/bin", allow_read)
        self.assertNotIn("/opt/ccc-bridge/venv", allow_read)
        self.assertNotIn("/opt/claude", allow_read)

    def test_shell_syntax_does_not_change_the_os_boundary(self):
        sandbox = tool_policy.strict_bash_sandbox_settings(Path("/srv/project"))
        attack_forms = [
            'P=/etc/passwd; cat "$P"',
            "python -c 'open(\"/etc/passwd\").read()'",
            "cd .. && pwd",
            "cat $(printf /etc/passwd)",
            "ln -s /etc/passwd ./outside && cat ./outside",
        ]

        for command in attack_forms:
            with self.subTest(command=command):
                self.assertEqual(sandbox["filesystem"]["denyRead"], ["/"])
                self.assertFalse(sandbox["allowUnsandboxedCommands"])


class ToolPolicyTest(unittest.TestCase):
    def test_default_auto_approves_bash(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(tool_policy.resolve_bash_policy(), "auto-approve")
            self.assertIn("Bash", tool_policy.allowed_tools())
            self.assertNotIn("Bash", tool_policy.disallowed_tools())
            self.assertIn("AskUserQuestion", tool_policy.disallowed_tools())
            self.assertEqual(tool_policy.bash_permission_hooks(), {})

    def test_auto_approve_normalizes_underscore_form(self):
        self.assertEqual(tool_policy.resolve_bash_policy("auto_approve"), "auto-approve")
        self.assertIn("Bash", tool_policy.allowed_tools("auto_approve"))

    def test_auto_review_is_recognized_but_keeps_claude_per_call_approval(self):
        self.assertEqual(tool_policy.resolve_bash_policy("auto_review"), "auto-review")
        self.assertNotIn("Bash", tool_policy.allowed_tools("auto-review"))
        hooks = tool_policy.bash_permission_hooks("auto-review")
        self.assertEqual(set(hooks), {"PreToolUse"})
        self.assertTrue(
            tool_policy.missing_callback_requires_denial("Bash", "auto-review")
        )

    def test_explicit_disabled_policy_hard_denies_bash(self):
        with patch.dict(os.environ, {"CCC_BRIDGE_BASH_POLICY": "disabled"}, clear=True):
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
        self.assertTrue(tool_policy.missing_callback_requires_denial("Bash", "approve-each"))
        self.assertFalse(tool_policy.missing_callback_requires_denial("Bash", "auto-approve"))
        self.assertTrue(tool_policy.missing_callback_requires_denial("Bash", "disabled"))
        self.assertFalse(tool_policy.missing_callback_requires_denial("Read", "disabled"))


class _MemorySessionManager:
    def __init__(self):
        self.sessions = {}

    async def get_session(self, key):
        return dict(self.sessions.setdefault(key, {}))

    async def update_session(self, key, value):
        self.sessions.setdefault(key, {}).update(value)

    async def replace_session(self, key, value):
        self.sessions[key] = dict(value)

    async def patch_session(self, key, *, updates=None, remove_fields=()):
        current = dict(self.sessions.setdefault(key, {}))
        current.update(updates or {})
        for field in remove_fields:
            current.pop(field, None)
        self.sessions[key] = current

    async def patch_session_if(
        self, key, *, expected, updates=None, remove_fields=()
    ):
        current = dict(self.sessions.setdefault(key, {}))
        if any(field not in current or current[field] != value for field, value in expected.items()):
            return False
        current.update(updates or {})
        for field in remove_fields:
            current.pop(field, None)
        self.sessions[key] = current
        return True

    async def set_pending_question(self, key, prompt):
        self.sessions.setdefault(key, {})["pending_question"] = prompt


class _AccessSubject(BotAccessMixin):
    _ALLOW_OUTSIDE_ONCE_TOKEN = "ALLOW_OUTSIDE_ONCE"
    _DENY_OUTSIDE_TOKEN = "DENY_OUTSIDE"

    def __init__(self, bash_policy, session_manager):
        self._test_bash_policy = bash_policy
        self._session_manager = session_manager

    @staticmethod
    def _conversation_key(user_id, chat_id=None):
        return f"{user_id}:{chat_id}" if chat_id is not None else str(user_id)

    def _bash_policy(self):
        return self._test_bash_policy


class _InjectedAccessSubject(BotAccessMixin):
    _ALLOW_OUTSIDE_ONCE_TOKEN = "ALLOW_OUTSIDE_ONCE"
    _DENY_OUTSIDE_TOKEN = "DENY_OUTSIDE"

    def __init__(self, settings, session_manager):
        self._config = settings
        self._session_manager = session_manager

    @staticmethod
    def _conversation_key(user_id, chat_id=None):
        return f"{user_id}:{chat_id}" if chat_id is not None else str(user_id)


class BashPermissionFlowTest(unittest.TestCase):
    def setUp(self):
        self.sessions = _MemorySessionManager()

    def subject(self, bash_policy):
        return _AccessSubject(bash_policy, self.sessions)

    def call(self, subject, command):
        return asyncio.run(subject._permission_callback(10, 20, "Bash", {"command": command}))

    def test_injected_disabled_policy_wins_over_ambient_auto_approve(self):
        settings = SimpleNamespace(
            bash_policy="disabled",
            execution_profile="strict-project",
            allowed_user_ids=[20],
            require_allowlist=True,
            project_root=Path("/tmp/injected-project"),
        )
        subject = _InjectedAccessSubject(settings, self.sessions)
        with patch.object(project_chat, "BASH_POLICY", "auto-approve"):
            result = self.call(subject, "pwd")
        self.assertIsInstance(result, PermissionResultDeny)

    def test_disabled_policy_denies_even_if_callback_is_called(self):
        result = self.call(self.subject("disabled"), "pwd")
        self.assertIsInstance(result, PermissionResultDeny)
        self.assertIn("disabled", result.message.lower())

    def test_auto_approve_policy_allows_without_one_time_token(self):
        result = self.call(self.subject("auto-approve"), "pwd")
        self.assertIsInstance(result, PermissionResultAllow)

    def test_every_per_call_bash_form_requires_approval(self):
        subject = self.subject("approve-each")
        commands = [
            "pwd",
            'P=/etc/passwd; cat "$P"',
            "python -c 'open(\"/etc/passwd\").read()'",
            "cd .. && pwd",
            "cat $(printf /etc/passwd)",
        ]
        for command in commands:
            with self.subTest(command=command):
                result = self.call(subject, command)
                self.assertIsInstance(result, PermissionResultDeny)
                self.assertIn("Bash", result.message)

    def test_one_time_approval_is_consumed_and_logged(self):
        subject = self.subject("approve-each")
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
        subject = self.subject("approve-each")
        key = subject._conversation_key(20, 10)
        self.sessions.sessions[key] = {
            "bash_approved_once": True,
            "bash_approved_digest": subject._approval_digest("Bash", {"command": "pwd"}),
            "pending_approval_kind": "bash",
        }
        with self.assertLogs("telegram_bot.core.bot_access", level="WARNING") as logs:
            result = self.call(subject, "cat /etc/passwd")
        self.assertIsInstance(result, PermissionResultDeny)
        self.assertFalse(self.sessions.sessions[key]["bash_approved_once"])
        self.assertNotIn("bash_approved_digest", self.sessions.sessions[key])
        self.assertTrue(any("bash_approval_digest_mismatch" in line for line in logs.output))

    def test_outside_path_approval_cannot_authorize_bash(self):
        subject = self.subject("approve-each")
        key = subject._conversation_key(20, 10)
        self.sessions.sessions[key] = {
            "outside_path_approved_once": True,
            "pending_approval_kind": "outside-path",
        }
        result = self.call(subject, "pwd")
        self.assertIsInstance(result, PermissionResultDeny)
        self.assertTrue(self.sessions.sessions[key]["outside_path_approved_once"])

    def test_allow_reply_creates_bash_specific_token(self):
        subject = self.subject("approve-each")
        key = subject._conversation_key(20, 10)
        self.sessions.sessions[key] = {
            "pending_outside_paths": ["Bash command requires per-call approval"],
            "pending_approval_kind": "bash",
            "pending_approval_digest": subject._approval_digest("Bash", {"command": "pwd"}),
        }
        asyncio.run(subject._maybe_capture_outside_approval(20, "ALLOW_OUTSIDE_ONCE", 10))
        self.assertTrue(self.sessions.sessions[key]["bash_approved_once"])
        self.assertTrue(self.sessions.sessions[key]["bash_approved_digest"])
        self.assertFalse(self.sessions.sessions[key].get("outside_path_approved_once", False))

    def test_button_reply_creates_token_only_in_the_matching_chat(self):
        subject = self.subject("approve-each")
        key = subject._conversation_key(20, 10)
        self.sessions.sessions[key] = {
            "pending_outside_paths": ["Bash command requires per-call approval"],
            "pending_approval_kind": "bash",
            "pending_approval_digest": subject._approval_digest("Bash", {"command": "pwd"}),
        }
        choice = "1. ALLOW_OUTSIDE_ONCE (Allow this Bash call once)"
        asyncio.run(subject._maybe_capture_outside_approval(20, choice, 11))
        self.assertFalse(self.sessions.sessions[key].get("bash_approved_once", False))
        asyncio.run(subject._maybe_capture_outside_approval(20, choice, 10))
        self.assertTrue(self.sessions.sessions[key]["bash_approved_once"])

    def test_approval_token_must_be_an_explicit_reply(self):
        subject = self.subject("approve-each")
        key = subject._conversation_key(20, 10)
        self.sessions.sessions[key] = {
            "pending_outside_paths": ["Bash command requires per-call approval"],
            "pending_approval_kind": "bash",
            "pending_approval_digest": subject._approval_digest("Bash", {"command": "pwd"}),
        }
        for text in (
            "do not use ALLOW_OUTSIDE_ONCE here",
            "1",
            "yes",
            "allow",
        ):
            with self.subTest(text=text):
                asyncio.run(subject._maybe_capture_outside_approval(20, text, 10))
                self.assertFalse(self.sessions.sessions[key].get("bash_approved_once", False))
                self.assertIn("pending_outside_paths", self.sessions.sessions[key])

    def test_denial_reply_is_logged_and_clears_pending_digest(self):
        subject = self.subject("approve-each")
        key = subject._conversation_key(20, 10)
        self.sessions.sessions[key] = {
            "pending_outside_paths": ["Bash command requires per-call approval"],
            "pending_approval_kind": "bash",
            "pending_approval_digest": subject._approval_digest("Bash", {"command": "pwd"}),
        }
        with self.assertLogs("telegram_bot.core.bot_access", level="INFO") as logs:
            asyncio.run(subject._maybe_capture_outside_approval(20, "2. DENY_OUTSIDE (Deny)", 10))
        self.assertFalse(self.sessions.sessions[key].get("bash_approved_once", False))
        self.assertNotIn("pending_approval_digest", self.sessions.sessions[key])
        self.assertTrue(any("bash_approval_reply_denied" in line for line in logs.output))

    def test_denial_records_pending_state_and_telemetry(self):
        subject = self.subject("approve-each")
        key = subject._conversation_key(20, 10)
        with self.assertLogs("telegram_bot.core.bot_access", level="WARNING") as logs:
            result = self.call(subject, "pwd")
        self.assertIsInstance(result, PermissionResultDeny)
        self.assertTrue(self.sessions.sessions[key]["pending_outside_paths"])
        self.assertEqual(self.sessions.sessions[key]["pending_approval_kind"], "bash")
        self.assertTrue(any("bash_approval_required" in line for line in logs.output))


if __name__ == "__main__":
    unittest.main()
