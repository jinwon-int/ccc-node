import asyncio
import importlib
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
setattr(sdk_module, "ClaudeSDKClient", type("ClaudeSDKClient", (), {}))
setattr(
    sdk_module,
    "ClaudeAgentOptions",
    type("ClaudeAgentOptions", (), {"__init__": lambda self, **kwargs: None}),
)
setattr(sdk_module, "AssistantMessage", type("AssistantMessage", (), {}))
setattr(sdk_module, "ResultMessage", type("ResultMessage", (), {}))
setattr(sdk_module, "StreamEvent", type("StreamEvent", (), {}))
setattr(sdk_module, "TextBlock", type("TextBlock", (), {}))
setattr(sdk_module, "ToolUseBlock", type("ToolUseBlock", (), {}))
setattr(sdk_module, "PermissionResultAllow", type("PermissionResultAllow", (), {}))
setattr(sdk_module, "PermissionResultDeny", type("PermissionResultDeny", (), {}))
sys.modules["claude_agent_sdk"] = sdk_module

internal_module = types.ModuleType("claude_agent_sdk._internal")
transport_pkg = types.ModuleType("claude_agent_sdk._internal.transport")
subprocess_cli_module = types.ModuleType("claude_agent_sdk._internal.transport.subprocess_cli")
setattr(subprocess_cli_module, "SubprocessCLITransport", type("SubprocessCLITransport", (), {}))
sys.modules["claude_agent_sdk._internal"] = internal_module
sys.modules["claude_agent_sdk._internal.transport"] = transport_pkg
sys.modules["claude_agent_sdk._internal.transport.subprocess_cli"] = subprocess_cli_module

config_module = types.ModuleType("telegram_bot.utils.config")
setattr(
    config_module,
    "config",
    SimpleNamespace(
        claude_cli_path=None,
        enable_streaming=False,
        bot_data_dir=None,
        task_ledger_path=None,
    ),
)
sys.modules["telegram_bot.utils.config"] = config_module

chat_logger_module = types.ModuleType("telegram_bot.utils.chat_logger")
setattr(chat_logger_module, "log_chat", lambda *args, **kwargs: None)
sys.modules["telegram_bot.utils.chat_logger"] = chat_logger_module

health_module = types.ModuleType("telegram_bot.utils.health")
setattr(
    health_module,
    "health_reporter",
    SimpleNamespace(
        record_claude_error=lambda *args, **kwargs: None,
        record_claude_ok=lambda *args, **kwargs: None,
    ),
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


class _SerialClient:
    def __init__(self, state):
        self.state = state
        self.queries = []
        self.first_query_started = asyncio.Event()
        self.second_query_started = asyncio.Event()

    async def query(self, message, session_id="default"):
        self.queries.append((message, session_id, len(self.state.pending)))
        if message == "first":
            self.first_query_started.set()
            return
        self.second_query_started.set()
        req = self.state.pending[-1]
        if not req.future.done():
            req.future.set_result(
                project_chat.ChatResponse(content="second done", session_id=session_id)
            )
        self.state.pending.popleft()


class ProjectChatSerializationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        sys.modules["telegram_bot.core.project_chat"] = project_chat
        self.handler = project_chat.ProjectChatHandler()
        self.state = project_chat._UserStreamState(client=None, model=None)
        self.client = _SerialClient(self.state)
        self.state.client = self.client

        async def get_state(user_id, chat_id, model, new_session):
            del user_id, chat_id, model, new_session
            return self.state

        self.handler._get_or_create_stream = get_state

    async def test_same_conversation_waits_for_result_before_next_query(self):
        first_task = asyncio.create_task(
            self.handler.process_message("first", user_id=7, chat_id=70)
        )
        await asyncio.wait_for(self.client.first_query_started.wait(), timeout=1.0)

        second_task = asyncio.create_task(
            self.handler.process_message("second", user_id=7, chat_id=70)
        )
        await asyncio.sleep(0.05)

        self.assertEqual(
            self.client.queries,
            [("first", "default", 1)],
            "second query must not be submitted while first result is pending",
        )
        self.assertFalse(self.client.second_query_started.is_set())
        self.assertEqual(len(self.state.pending), 1)

        first_req = self.state.pending[0]
        first_req.future.set_result(
            project_chat.ChatResponse(content="first done", session_id="s1")
        )
        self.state.pending.popleft()

        first = await asyncio.wait_for(first_task, timeout=1.0)
        await asyncio.wait_for(self.client.second_query_started.wait(), timeout=1.0)
        second = await asyncio.wait_for(second_task, timeout=1.0)

        self.assertEqual(first.content, "first done")
        self.assertEqual(second.content, "second done")
        self.assertEqual(
            [message for message, _session, _pending in self.client.queries],
            ["first", "second"],
        )
        self.assertEqual(
            self.client.queries[1][2],
            1,
            "second request should be the only pending bridge request when submitted",
        )
        self.assertEqual(len(self.state.pending), 0)


if __name__ == "__main__":
    unittest.main()
