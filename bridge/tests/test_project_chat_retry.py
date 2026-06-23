# pyright: reportMissingImports=false, reportAttributeAccessIssue=false, reportOptionalMemberAccess=false
# ruff: noqa: E402
import asyncio
import os
import sys
import types
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

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
sdk_module.AssistantMessage = type("AssistantMessage", (), {})
sdk_module.ResultMessage = type("ResultMessage", (), {})
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

config_module = types.ModuleType("telegram_bot.utils.config")
config_module.config = SimpleNamespace(claude_cli_path=None)
sys.modules["telegram_bot.utils.config"] = config_module

chat_logger_module = types.ModuleType("telegram_bot.utils.chat_logger")
chat_logger_module.log_chat = lambda *args, **kwargs: None
sys.modules["telegram_bot.utils.chat_logger"] = chat_logger_module

health_module = types.ModuleType("telegram_bot.utils.health")
health_module.health_reporter = SimpleNamespace(
    record_claude_error=lambda *args, **kwargs: None,
    record_claude_ok=lambda *args, **kwargs: None,
)
sys.modules["telegram_bot.utils.health"] = health_module

sys.modules.pop("telegram_bot.core.project_chat", None)
from telegram_bot.core import project_chat


class _FailingRetryClient:
    def __init__(self, state):
        self.state = state
        self.request = None

    async def query(self, *_args, **_kwargs):
        self.request = self.state.pending[0]
        raise RuntimeError("retry query failed")


class RetryCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_failure_removes_pending_and_cancels_future(self):
        handler = project_chat.ProjectChatHandler()
        retry_state = SimpleNamespace(
            send_lock=asyncio.Lock(),
            pending=deque(),
            client=None,
            last_session_id=None,
        )
        retry_client = _FailingRetryClient(retry_state)
        retry_state.client = retry_client

        calls = 0

        async def get_or_create_stream(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("connection reset")
            return retry_state

        handler._get_or_create_stream = get_or_create_stream
        handler._disconnect_user_stream = AsyncMock(return_value=True)

        response = await handler.process_message("hello", user_id=7, chat_id=42)

        self.assertFalse(response.success)
        self.assertIn("retry query failed", response.error)
        self.assertEqual(len(retry_state.pending), 0)
        self.assertIsNotNone(retry_client.request)
        self.assertTrue(retry_client.request.future.cancelled())


if __name__ == "__main__":
    unittest.main()
