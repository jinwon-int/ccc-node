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


class _FakeStreamingHandler:
    """Records draft updates without touching Telegram."""

    def __init__(self):
        self.updates = []
        self.drafts = [object()]  # non-empty -> response marked as streamed
        self.finalized = False

    async def update_if_needed(self, text):
        self.updates.append(text)

    async def finalize_all(self):
        self.finalized = True


class _ScriptedClient:
    """Async client whose receive_messages() replays a fixed message script."""

    def __init__(self, messages):
        self._messages = messages

    async def receive_messages(self):
        for msg in self._messages:
            yield msg


def _new(cls, **attrs):
    """Build an SDK message instance without invoking its constructor.

    The reader-loop tests must work whether ``project_chat`` imported the bare
    stub types (this module run in isolation) or the real claude_agent_sdk
    dataclasses (full pytest run, where another test imported the real SDK
    first). Bypassing __init__ and setting only the attributes the reader loop
    reads keeps the helpers valid for both. (None of these dataclasses use
    __slots__, so setattr works.)
    """
    obj = object.__new__(cls)
    for key, value in attrs.items():
        setattr(obj, key, value)
    return obj


def _stream_event(text, parent=None):
    return _new(
        project_chat.StreamEvent,
        event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}},
        parent_tool_use_id=parent,
    )


def _assistant(text):
    return _new(
        project_chat.AssistantMessage,
        content=[_new(project_chat.TextBlock, text=text)],
    )


def _result(text, session_id="sess-1"):
    return _new(
        project_chat.ResultMessage,
        result=text,
        session_id=session_id,
        is_error=False,
        duration_ms=5,
    )


def _make_request(streaming_handler):
    loop = asyncio.get_event_loop()
    return project_chat._PendingRequest(
        user_id=7,
        chat_id=42,
        model=None,
        requested_session_id=None,
        permission_callback=None,
        typing_callback=None,
        future=loop.create_future(),
        streaming_handler=streaming_handler,
    )


class _DeltaHelperTests(unittest.TestCase):
    def test_extracts_text_delta(self):
        ev = {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hi"}}
        self.assertEqual(project_chat._extract_stream_text_delta(ev), "hi")

    def test_ignores_non_text_events(self):
        for ev in (
            {"type": "content_block_delta", "delta": {"type": "input_json_delta", "partial_json": "{"}},
            {"type": "content_block_start"},
            {"type": "message_stop"},
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": ""}},
            "not-a-dict",
            None,
        ):
            self.assertIsNone(project_chat._extract_stream_text_delta(ev))


class PartialStreamingReaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_deltas_drive_draft_and_block_not_redelivered(self):
        handler = project_chat.ProjectChatHandler()
        sh = _FakeStreamingHandler()
        req = _make_request(sh)
        state = project_chat._UserStreamState(client=None, model=None)
        state.pending.append(req)
        state.client = _ScriptedClient(
            [
                _stream_event("Hello "),
                _stream_event("world"),
                _stream_event(" [sub]", parent="tool_123"),  # subagent — must be ignored
                _assistant("Hello world"),  # complete block — must NOT be re-fed
                _result("Hello world"),
            ]
        )

        await handler._reader_loop(7, state)

        # Only the two top-level deltas drove the draft; the subagent delta and
        # the complete AssistantMessage block were not fed (no double-count).
        self.assertEqual(sh.updates, ["Hello ", "world"])
        self.assertTrue(sh.finalized)
        self.assertTrue(req.streamed_via_partials)
        resp = req.future.result()
        self.assertTrue(resp.success)
        self.assertEqual(resp.content, "Hello world")
        self.assertTrue(resp.streamed)

    async def test_falls_back_to_block_when_no_deltas(self):
        handler = project_chat.ProjectChatHandler()
        sh = _FakeStreamingHandler()
        req = _make_request(sh)
        state = project_chat._UserStreamState(client=None, model=None)
        state.pending.append(req)
        state.client = _ScriptedClient([_assistant("Hi there"), _result("Hi there")])

        await handler._reader_loop(7, state)

        # No StreamEvents arrived, so the complete block drives the draft.
        self.assertEqual(sh.updates, ["Hi there"])
        self.assertFalse(req.streamed_via_partials)
        self.assertEqual(req.future.result().content, "Hi there")


if __name__ == "__main__":
    unittest.main()
