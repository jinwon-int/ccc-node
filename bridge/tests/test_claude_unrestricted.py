"""Opt-in Codex-parity ungoverned Claude execution (CCC_BRIDGE_CLAUDE_UNRESTRICTED).

Verifies the flag resolves to enabled only on owner-operator and is
fail-closed everywhere else (including under root, where Claude Code refuses
bypassPermissions).
"""

import unittest
from unittest.mock import patch

from telegram_bot.core import project_chat, tool_policy
from telegram_bot.utils.config import Config


def _handler_flag(*, profile: str, unrestricted: bool, is_root: bool = False) -> bool:
    with (
        patch.object(project_chat, "EXECUTION_PROFILE", profile, create=True),
        patch.object(project_chat, "CLAUDE_UNRESTRICTED", unrestricted),
        patch.object(project_chat, "running_as_root", return_value=is_root),
    ):
        return project_chat.ProjectChatHandler()._claude_unrestricted


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
    def test_owner_operator_flag_resolves_enabled(self) -> None:
        self.assertTrue(_handler_flag(profile="owner-operator", unrestricted=True))

    def test_owner_operator_default_keeps_the_guard_boundary(self) -> None:
        self.assertFalse(_handler_flag(profile="owner-operator", unrestricted=False))

    def test_flag_is_ignored_on_strict_project(self) -> None:
        self.assertFalse(_handler_flag(profile="strict-project", unrestricted=True))

    def test_flag_is_ignored_on_disabled_profile(self) -> None:
        self.assertFalse(_handler_flag(profile="disabled", unrestricted=True))

    def test_flag_is_ignored_under_root_and_keeps_the_guard(self) -> None:
        # Root bridge: Claude Code refuses bypassPermissions, so the flag must
        # degrade to the normal guarded owner-operator path (and warn).
        with self.assertLogs(project_chat.logger, level="WARNING") as logs:
            self.assertFalse(
                _handler_flag(profile="owner-operator", unrestricted=True, is_root=True)
            )
        self.assertTrue(any("ignored under root" in line for line in logs.output))


class ClaudeUnrestrictedDefaultContractTest(unittest.TestCase):
    def test_default_remains_guarded_opt_in(self) -> None:
        """Owner-operator parity must not silently disable the host guard."""
        self.assertIs(Config.model_fields["claude_unrestricted"].default, False)


if __name__ == "__main__":
    unittest.main()
