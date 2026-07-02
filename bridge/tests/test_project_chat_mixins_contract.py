"""Compatibility contract tests for ProjectChatHandler mixin extraction."""

import importlib
import json
import os
import sys
import tempfile
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
sdk_module.ClaudeSDKClient = type("ClaudeSDKClient", (), {})
sdk_module.ClaudeAgentOptions = type("ClaudeAgentOptions", (), {"__init__": lambda self, **kwargs: None})
sdk_module.AssistantMessage = type("AssistantMessage", (), {})
sdk_module.ResultMessage = type("ResultMessage", (), {})
sdk_module.StreamEvent = type("StreamEvent", (), {})
sdk_module.TextBlock = type("TextBlock", (), {})
sdk_module.ToolUseBlock = type("ToolUseBlock", (), {})
sdk_module.PermissionResultAllow = type("PermissionResultAllow", (), {})
sdk_module.PermissionResultDeny = type("PermissionResultDeny", (), {})
sys.modules["claude_agent_sdk"] = sdk_module

internal_module = types.ModuleType("claude_agent_sdk._internal")
transport_pkg = types.ModuleType("claude_agent_sdk._internal.transport")
subprocess_cli_module = types.ModuleType("claude_agent_sdk._internal.transport.subprocess_cli")
subprocess_cli_module.SubprocessCLITransport = type("SubprocessCLITransport", (), {})
sys.modules["claude_agent_sdk._internal"] = internal_module
sys.modules["claude_agent_sdk._internal.transport"] = transport_pkg
sys.modules["claude_agent_sdk._internal.transport.subprocess_cli"] = subprocess_cli_module

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

for name in [
    "telegram_bot.core.project_chat",
    "telegram_bot.core.project_chat_history",
    "telegram_bot.core.project_chat_process",
    "telegram_bot.core.project_chat_reader",
    "telegram_bot.core.project_chat_state",
]:
    sys.modules.pop(name, None)

project_chat = importlib.import_module("telegram_bot.core.project_chat")
project_chat_history = importlib.import_module("telegram_bot.core.project_chat_history")
project_chat_process = importlib.import_module("telegram_bot.core.project_chat_process")
project_chat_reader = importlib.import_module("telegram_bot.core.project_chat_reader")


class ProjectChatMixinContractTests(unittest.TestCase):
    def setUp(self):
        # Some legacy tests install a lightweight fake project_chat module in
        # sys.modules. These contract tests exercise the real module imported
        # above, so restore it as the active module before each assertion.
        sys.modules["telegram_bot.core.project_chat"] = project_chat

    def test_project_chat_handler_exposes_moved_private_api(self):
        handler = project_chat.ProjectChatHandler()
        moved_methods = [
            "_reader_loop",
            "process_message",
            "stop",
            "_states_for_user",
            "cancel_user_streaming",
            "inflight_count",
            "is_user_busy",
            "clear_user_stream",
            "clear_pending_permissions",
            "list_sessions",
            "get_session_last_assistant_message",
            "get_recent_messages",
            "get_conversation_history",
            "_extract_first_user_message",
            "_clean_response",
        ]
        for method_name in moved_methods:
            with self.subTest(method_name=method_name):
                self.assertTrue(callable(getattr(handler, method_name, None)))

    def test_mixin_helpers_read_compat_constants_at_call_time(self):
        original_typing = project_chat.TYPING_INTERVAL
        original_timeout = project_chat.PROCESS_TIMEOUT
        try:
            project_chat.TYPING_INTERVAL = 0.123
            project_chat.PROCESS_TIMEOUT = 456
            self.assertEqual(project_chat_reader._typing_interval(), 0.123)
            self.assertEqual(project_chat_process._process_timeout(), 456)
        finally:
            project_chat.TYPING_INTERVAL = original_typing
            project_chat.PROCESS_TIMEOUT = original_timeout

    def test_history_helpers_read_conversations_dir_at_call_time(self):
        original_dir = project_chat.CONVERSATIONS_DIR
        try:
            with tempfile.TemporaryDirectory() as tmp:
                conv_dir = Path(tmp)
                project_chat.CONVERSATIONS_DIR = conv_dir
                session_path = conv_dir / "session-1.jsonl"
                session_path.write_text(
                    json.dumps(
                        {
                            "type": "user",
                            "message": {"role": "user", "content": "hello from patched dir"},
                            "timestamp": "2026-07-03T00:00:00Z",
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                handler = project_chat.ProjectChatHandler()
                self.assertEqual(project_chat_history._conversations_dir(), conv_dir)
                self.assertEqual(handler.list_sessions(limit=1)[0][0], "session-1")
                self.assertEqual(
                    handler.get_conversation_history("session-1", limit=1)[0]["content"],
                    "hello from patched dir",
                )
        finally:
            project_chat.CONVERSATIONS_DIR = original_dir


if __name__ == "__main__":
    unittest.main()
