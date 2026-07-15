# pyright: reportMissingImports=false, reportAttributeAccessIssue=false, reportOptionalMemberAccess=false
# ruff: noqa: E402
"""Tests for the bridge-restart (exit 143) guard in project_chat.

When ``ccc-telegram-bridge.service`` is restarted while a reply is streaming,
systemd delivers SIGTERM to the whole cgroup and the in-flight claude child dies
with exit 143 (=128+SIGTERM). Previously that raw error was sent to the user as
"❌ Error: ... exit 143". These tests lock in the new behaviour:
  - such errors are classified as shutdown-signal errors,
  - they are treated as retryable,
  - the user-facing content becomes a friendly "please resend" notice.
"""
import os
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

BRIDGE_DIR = Path(__file__).resolve().parents[1]
os.environ.setdefault("PROJECT_ROOT", str(BRIDGE_DIR))

telegram_bot_pkg = types.ModuleType("telegram_bot")
telegram_bot_pkg.__path__ = [str(BRIDGE_DIR)]
sys.modules.setdefault("telegram_bot", telegram_bot_pkg)

sdk_module = types.ModuleType("claude_agent_sdk")


class _DummySDKClient:
    pass


class _DummyAgentOptions:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _PermissionResultAllow:
    pass


class _PermissionResultDeny:
    pass


sdk_module.ClaudeSDKClient = _DummySDKClient
sdk_module.ClaudeAgentOptions = _DummyAgentOptions
sdk_module.HookMatcher = type(
    "HookMatcher", (), {"__init__": lambda self, **kwargs: None}
)
sdk_module.AssistantMessage = type("AssistantMessage", (), {})
sdk_module.RateLimitEvent = type("RateLimitEvent", (), {})
sdk_module.ResultMessage = type("ResultMessage", (), {})
sdk_module.StreamEvent = type("StreamEvent", (), {})
sdk_module.TextBlock = type("TextBlock", (), {})
sdk_module.ToolUseBlock = type("ToolUseBlock", (), {})
sdk_module.PermissionResultAllow = _PermissionResultAllow
sdk_module.PermissionResultDeny = _PermissionResultDeny
sys.modules.setdefault("claude_agent_sdk", sdk_module)

internal_module = types.ModuleType("claude_agent_sdk._internal")
transport_pkg = types.ModuleType("claude_agent_sdk._internal.transport")
subprocess_cli_module = types.ModuleType(
    "claude_agent_sdk._internal.transport.subprocess_cli"
)
subprocess_cli_module.SubprocessCLITransport = type("SubprocessCLITransport", (), {})
sys.modules.setdefault("claude_agent_sdk._internal", internal_module)
sys.modules.setdefault("claude_agent_sdk._internal.transport", transport_pkg)
sys.modules.setdefault(
    "claude_agent_sdk._internal.transport.subprocess_cli", subprocess_cli_module
)

# Non-destructive stubs (setdefault) so this file does not clobber state set up
# by sibling test modules that also stub these — these tests only exercise
# module-level helpers, so no fresh project_chat import is needed.
config_module = types.ModuleType("telegram_bot.utils.config")
config_module.config = SimpleNamespace(claude_cli_path=None)
sys.modules.setdefault("telegram_bot.utils.config", config_module)

chat_logger_module = types.ModuleType("telegram_bot.utils.chat_logger")
chat_logger_module.log_chat = lambda *args, **kwargs: None
sys.modules.setdefault("telegram_bot.utils.chat_logger", chat_logger_module)

health_module = types.ModuleType("telegram_bot.utils.health")
health_module.health_reporter = SimpleNamespace(
    record_claude_error=lambda *args, **kwargs: None,
    record_claude_ok=lambda *args, **kwargs: None,
)
sys.modules.setdefault("telegram_bot.utils.health", health_module)

from telegram_bot.core import project_chat


class ShutdownSignalDetectionTests(unittest.TestCase):
    def test_detects_signal_terminations(self):
        cases = [
            "Command exited with code 143",
            "process exited with status 137",
            "claude CLI exit code 143",
            "exit code -15",
            "exit code -9",
            "Killed by SIGTERM",
            "received SIGKILL",
            "Stopped by signal",
        ]
        for msg in cases:
            with self.subTest(msg=msg):
                self.assertTrue(project_chat._is_shutdown_signal_error(msg))

    def test_ignores_unrelated_errors(self):
        cases = [
            "Invalid token",
            "Permission denied",
            "TypeError: bad operand",
            "exit code 1",
            "exit code 2",
            "some normal failure",
            "",
        ]
        for msg in cases:
            with self.subTest(msg=msg):
                self.assertFalse(project_chat._is_shutdown_signal_error(msg))


class RetryableClassificationTests(unittest.TestCase):
    def test_shutdown_signal_is_retryable(self):
        self.assertTrue(
            project_chat._is_retryable_sdk_error(
                RuntimeError("Command exited with code 143")
            )
        )
        self.assertTrue(
            project_chat._is_retryable_sdk_error(RuntimeError("terminated by SIGKILL"))
        )

    def test_permanent_error_not_retryable(self):
        self.assertFalse(
            project_chat._is_retryable_sdk_error(ValueError("bad value"))
        )
        self.assertFalse(
            project_chat._is_retryable_sdk_error(RuntimeError("Invalid token"))
        )


class RestartNoticeTests(unittest.TestCase):
    def test_notice_is_friendly_and_non_alarming(self):
        notice = project_chat.RESTART_INTERRUPT_NOTICE
        self.assertIn("resend", notice.lower())
        self.assertNotIn("143", notice)
        self.assertNotIn("❌", notice)


if __name__ == "__main__":
    unittest.main()
