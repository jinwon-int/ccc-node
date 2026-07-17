"""Opt-in Codex-parity ungoverned Claude execution (CCC_BRIDGE_CLAUDE_UNRESTRICTED).

Verifies the flag reaches Codex parity (no guard settings chain, bypass
permissions, no OS sandbox) only on owner-operator, and is fail-closed
everywhere else.
"""

import asyncio
import unittest
from unittest.mock import patch

from telegram_bot.core import project_chat, tool_policy


class _FakeSDKClient:
    last_options = None

    def __init__(self, options):
        type(self).last_options = options

    async def connect(self):
        return None


def _close_task(coro):
    coro.close()
    return object()


def _build_options(
    *, profile: str, policy: str = "auto-approve", unrestricted: bool, is_root: bool = False
):
    _FakeSDKClient.last_options = None
    with (
        patch.object(project_chat, "EXECUTION_PROFILE", profile, create=True),
        patch.object(project_chat, "BASH_POLICY", policy),
        patch.object(project_chat, "CLAUDE_UNRESTRICTED", unrestricted),
        patch.object(project_chat, "running_as_root", return_value=is_root),
        patch.object(project_chat, "ClaudeSDKClient", _FakeSDKClient),
        patch.object(project_chat.asyncio, "create_task", side_effect=_close_task),
    ):
        asyncio.run(project_chat.ProjectChatHandler()._create_user_stream(10, None))
    options = _FakeSDKClient.last_options
    assert options is not None
    return options


class ClaudeUnrestrictedGateTest(unittest.TestCase):
    def test_gate_requires_owner_operator(self) -> None:
        self.assertTrue(
            tool_policy.claude_unrestricted_enabled(True, "owner-operator")
        )
        # Fail-closed everywhere else, and only for an explicit boolean True.
        self.assertFalse(
            tool_policy.claude_unrestricted_enabled(True, "strict-project")
        )
        self.assertFalse(tool_policy.claude_unrestricted_enabled(True, "disabled"))
        self.assertFalse(
            tool_policy.claude_unrestricted_enabled(False, "owner-operator")
        )
        self.assertFalse(
            tool_policy.claude_unrestricted_enabled("true", "owner-operator")
        )
        self.assertFalse(
            tool_policy.claude_unrestricted_enabled(1, "owner-operator")
        )

    def test_gate_is_disabled_under_root(self) -> None:
        # Claude Code refuses bypassPermissions under root, so the flag must
        # degrade even on owner-operator to avoid bricking every new session.
        self.assertFalse(
            tool_policy.claude_unrestricted_enabled(
                True, "owner-operator", is_root=True
            )
        )
        self.assertTrue(
            tool_policy.claude_unrestricted_enabled(
                True, "owner-operator", is_root=False
            )
        )


class ClaudeUnrestrictedWiringTest(unittest.TestCase):
    def test_owner_operator_flag_reaches_codex_parity(self) -> None:
        options = _build_options(profile="owner-operator", unrestricted=True)
        self.assertEqual(options.permission_mode, "bypassPermissions")
        # No host settings chain => the PreToolUse guard hook is not loaded.
        self.assertEqual(options.setting_sources, [])
        # No OS sandbox: host-capable like Codex dangerFullAccess.
        self.assertIsNone(options.sandbox)
        # Bash stays auto-allowed.
        self.assertIn("Bash", options.allowed_tools)

    def test_owner_operator_default_keeps_the_guard_boundary(self) -> None:
        options = _build_options(profile="owner-operator", unrestricted=False)
        self.assertEqual(options.permission_mode, "default")
        self.assertEqual(options.setting_sources, ["user", "project", "local"])
        self.assertIsNone(options.sandbox)

    def test_flag_is_ignored_on_strict_project(self) -> None:
        options = _build_options(profile="strict-project", unrestricted=True)
        # Fail-closed: strict-project stays sandboxed and never bypasses.
        self.assertNotEqual(options.permission_mode, "bypassPermissions")
        self.assertEqual(options.setting_sources, [])
        self.assertIsNotNone(options.sandbox)

    def test_flag_is_ignored_when_bash_disabled_profile(self) -> None:
        options = _build_options(
            profile="disabled", policy="auto-approve", unrestricted=True
        )
        self.assertNotEqual(options.permission_mode, "bypassPermissions")

    def test_flag_is_ignored_under_root_and_keeps_the_guard(self) -> None:
        # Root bridge: Claude Code refuses bypassPermissions, so the flag must
        # degrade to the normal guarded owner-operator path instead of
        # emitting an option Claude Code would reject.
        options = _build_options(
            profile="owner-operator", unrestricted=True, is_root=True
        )
        self.assertEqual(options.permission_mode, "default")
        self.assertEqual(options.setting_sources, ["user", "project", "local"])


if __name__ == "__main__":
    unittest.main()
