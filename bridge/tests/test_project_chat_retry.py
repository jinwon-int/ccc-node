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

from sys_modules_isolation import ModuleFakesGuard

_sys_modules_guard = ModuleFakesGuard(__name__).begin()

telegram_bot_pkg = types.ModuleType("telegram_bot")
telegram_bot_pkg.__path__ = [str(BRIDGE_DIR)]
sys.modules.setdefault("telegram_bot", telegram_bot_pkg)

sdk_module = types.ModuleType("claude_agent_sdk")
sdk_module.__path__ = []


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

sdk_types_module = types.ModuleType("claude_agent_sdk.types")
sdk_types_module.HookContext = type("HookContext", (), {})
sdk_types_module.HookInput = type("HookInput", (), {})
sdk_types_module.HookJSONOutput = type("HookJSONOutput", (), {})
sdk_types_module.PermissionResultAllow = _PermissionResultAllow
sdk_types_module.PermissionResultDeny = _PermissionResultDeny
sys.modules.setdefault("claude_agent_sdk.types", sdk_types_module)

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

_sys_modules_guard.finish()


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
        self.finalized_segments = 0

    async def update_if_needed(self, text):
        if not self.drafts:
            self.drafts.append(object())
        self.updates.append(text)

    async def finalize_all(self):
        self.finalized = True
        return bool(self.drafts)

    async def finalize_segment(self):
        self.finalized_segments += 1
        self.drafts.clear()
        return True


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


def _assistant_with_tool(text, *, name="Bash", tool_input=None):
    return _new(
        project_chat.AssistantMessage,
        content=[
            _new(project_chat.TextBlock, text=text),
            _new(
                project_chat.ToolUseBlock,
                name=name,
                input=tool_input or {"command": "true"},
            ),
        ],
    )


def _result(text, session_id="sess-1", is_error=False):
    return _new(
        project_chat.ResultMessage,
        result=text,
        session_id=session_id,
        is_error=is_error,
        duration_ms=5,
    )


def _make_request(streaming_handler, interim_message_callback=None):
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
        interim_message_callback=interim_message_callback,
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


class ClaudeMessageBoundaryReaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_before_tool_is_delivered_as_interim_bubble(self):
        delivered = []

        async def deliver_interim(content):
            delivered.append(content)

        handler = project_chat.ProjectChatHandler()
        req = _make_request(None, deliver_interim)
        state = project_chat._UserStreamState(client=None, model=None)
        state.pending.append(req)
        state.client = _ScriptedClient(
            [
                _assistant_with_tool("I'll check the repository now."),
                _assistant("There are 17 open issues."),
                _result("There are 17 open issues."),
            ]
        )

        await handler._reader_loop(7, state)

        self.assertEqual(delivered, ["I'll check the repository now."])
        response = req.future.result()
        self.assertEqual(response.content, "There are 17 open issues.")
        self.assertFalse(response.streamed)

    async def test_next_assistant_message_proves_prior_message_is_interim(self):
        delivered = []

        async def deliver_interim(content):
            delivered.append(content)

        handler = project_chat.ProjectChatHandler()
        req = _make_request(None, deliver_interim)
        state = project_chat._UserStreamState(client=None, model=None)
        state.pending.append(req)
        state.client = _ScriptedClient(
            [
                _assistant("First completed message."),
                _assistant("Final completed message."),
                _result("Final completed message."),
            ]
        )

        await handler._reader_loop(7, state)

        self.assertEqual(delivered, ["First completed message."])
        self.assertEqual(req.future.result().content, "Final completed message.")

    async def test_terminal_assistant_message_stays_on_final_delivery_path(self):
        delivered = []

        async def deliver_interim(content):
            delivered.append(content)

        handler = project_chat.ProjectChatHandler()
        req = _make_request(None, deliver_interim)
        state = project_chat._UserStreamState(client=None, model=None)
        state.pending.append(req)
        state.client = _ScriptedClient(
            [_assistant("The final answer."), _result("The final answer.")]
        )

        await handler._reader_loop(7, state)

        self.assertEqual(delivered, [])
        self.assertEqual(req.future.result().content, "The final answer.")

    async def test_failed_interim_delivery_falls_back_to_complete_final_text(self):
        async def fail_interim(_content):
            raise OSError("telegram unavailable")

        handler = project_chat.ProjectChatHandler()
        req = _make_request(None, fail_interim)
        state = project_chat._UserStreamState(client=None, model=None)
        state.pending.append(req)
        state.client = _ScriptedClient(
            [
                _assistant_with_tool("First message."),
                _assistant("Final message."),
                _result("Final message."),
            ]
        )

        await handler._reader_loop(7, state)

        response = req.future.result()
        self.assertEqual(response.content, "First message.\n\nFinal message.")
        self.assertFalse(response.streamed)

    async def test_tool_only_tail_does_not_repeat_delivered_progress(self):
        delivered = []

        async def deliver_interim(content):
            delivered.append(content)

        handler = project_chat.ProjectChatHandler()
        req = _make_request(None, deliver_interim)
        state = project_chat._UserStreamState(client=None, model=None)
        state.pending.append(req)
        state.client = _ScriptedClient(
            [
                _assistant_with_tool("Only progress message."),
                _result("Only progress message."),
            ]
        )

        await handler._reader_loop(7, state)

        response = req.future.result()
        self.assertEqual(delivered, ["Only progress message."])
        self.assertEqual(response.content, "(No response)")
        self.assertTrue(response.streamed)

    async def test_streaming_finalizes_each_assistant_message_once(self):
        handler = project_chat.ProjectChatHandler()
        streaming = _FakeStreamingHandler()
        req = _make_request(streaming)
        state = project_chat._UserStreamState(client=None, model=None)
        state.pending.append(req)
        state.client = _ScriptedClient(
            [
                _stream_event("Checking now."),
                _assistant_with_tool("Checking now."),
                _stream_event("Final answer."),
                _assistant("Final answer."),
                _result("Final answer."),
            ]
        )

        await handler._reader_loop(7, state)

        self.assertEqual(streaming.updates, ["Checking now.", "Final answer."])
        self.assertEqual(streaming.finalized_segments, 1)
        self.assertTrue(streaming.finalized)
        self.assertTrue(req.future.result().streamed)

    async def test_later_message_without_partials_uses_block_fallback(self):
        handler = project_chat.ProjectChatHandler()
        streaming = _FakeStreamingHandler()
        req = _make_request(streaming)
        state = project_chat._UserStreamState(client=None, model=None)
        state.pending.append(req)
        state.client = _ScriptedClient(
            [
                _stream_event("Checking now."),
                _assistant_with_tool("Checking now."),
                _assistant("Final block-only answer."),
                _result("Final block-only answer."),
            ]
        )

        await handler._reader_loop(7, state)

        self.assertEqual(
            streaming.updates,
            ["Checking now.", "Final block-only answer."],
        )

    async def test_no_delivery_surface_preserves_legacy_final_only_behavior(self):
        handler = project_chat.ProjectChatHandler()
        req = _make_request(None)
        state = project_chat._UserStreamState(client=None, model=None)
        state.pending.append(req)
        state.client = _ScriptedClient(
            [
                _assistant_with_tool("Progress that voice mode should not split."),
                _assistant("Final answer."),
                _result("Final answer."),
            ]
        )

        await handler._reader_loop(7, state)

        self.assertEqual(req.future.result().content, "Final answer.")


class UnsolicitedReaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_assistant_result_pair_is_delivered_once_without_pending_request(self):
        delivered = []

        async def deliver(content, session_id):
            delivered.append((content, session_id))

        handler = project_chat.ProjectChatHandler()
        state = project_chat._UserStreamState(
            client=_ScriptedClient(
                [
                    _stream_event("ignored partial"),
                    _assistant("background task finished"),
                    _result("", session_id="task-session"),
                ]
            ),
            model=None,
            unsolicited_callback=deliver,
        )

        await handler._reader_loop(7, state)

        self.assertEqual(delivered, [("background task finished", "task-session")])
        self.assertEqual(state.last_session_id, "task-session")
        self.assertEqual(state.unsolicited_assistant_texts, [])
        self.assertFalse(state.unsolicited_inflight)

    async def test_unsolicited_turn_keeps_result_when_request_arrives_mid_turn(self):
        delivered = []

        async def deliver(content, session_id):
            delivered.append((content, session_id))

        handler = project_chat.ProjectChatHandler()
        request = _make_request(None)
        state = project_chat._UserStreamState(
            client=None,
            model=None,
            unsolicited_callback=deliver,
        )

        class _RacingClient:
            async def receive_messages(self):
                yield _assistant("background result")
                state.pending.append(request)
                yield _result("", session_id="background-session")

        state.client = _RacingClient()
        await handler._reader_loop(7, state)

        self.assertEqual(delivered, [("background result", "background-session")])
        self.assertEqual(list(state.pending), [request])
        self.assertFalse(request.future.done())
        self.assertFalse(state.unsolicited_inflight)

    async def test_multiple_assistant_messages_are_preserved_for_empty_result(self):
        delivered = []

        async def deliver(content, session_id):
            delivered.append((content, session_id))

        handler = project_chat.ProjectChatHandler()
        state = project_chat._UserStreamState(
            client=_ScriptedClient(
                [
                    _assistant("first phase"),
                    _assistant("second phase"),
                    _result("", session_id="task-session"),
                ]
            ),
            model=None,
            unsolicited_callback=deliver,
        )

        await handler._reader_loop(7, state)

        self.assertEqual(delivered, [("first phase\nsecond phase", "task-session")])

    async def test_unsolicited_error_records_error_health_not_ok(self):
        delivered = []

        async def deliver(content, session_id):
            delivered.append((content, session_id))

        handler = project_chat.ProjectChatHandler()
        state = project_chat._UserStreamState(
            client=_ScriptedClient(
                [_result("task failed", session_id="task-session", is_error=True)]
            ),
            model=None,
            unsolicited_callback=deliver,
        )
        reader_health = handler._handle_unsolicited_message.__func__.__globals__[
            "health_reporter"
        ]
        error_record = unittest.mock.Mock()
        ok_record = unittest.mock.Mock()
        old_error = reader_health.record_claude_error
        old_ok = reader_health.record_claude_ok
        reader_health.record_claude_error = error_record
        reader_health.record_claude_ok = ok_record
        try:
            await handler._reader_loop(7, state)
        finally:
            reader_health.record_claude_error = old_error
            reader_health.record_claude_ok = old_ok

        self.assertEqual(
            delivered,
            [("❌ Background task failed: task failed", "task-session")],
        )
        error_record.assert_called_once_with("❌ Background task failed: task failed")
        ok_record.assert_not_called()
        self.assertEqual(state.last_error, "❌ Background task failed: task failed")

    async def test_process_message_registers_bot_route_before_query(self):
        handler = project_chat.ProjectChatHandler()
        bot = SimpleNamespace(send_message=AsyncMock())
        state = project_chat._UserStreamState(client=None, model=None)

        class _ImmediateClient:
            async def query(self, *_args, **_kwargs):
                self_outer.assertIsNotNone(state.unsolicited_callback)
                await state.unsolicited_callback("late task done", "task-session")
                req = state.pending[0]
                req.future.set_result(project_chat.ChatResponse(content="ok"))

        async def get_stream(
            _user_id, _chat_id, _model, _new_session, unsolicited_callback
        ):
            state.unsolicited_callback = unsolicited_callback
            return state

        self_outer = self
        state.client = _ImmediateClient()
        handler._get_or_create_stream = AsyncMock(side_effect=get_stream)

        response = await handler.process_message(
            "hello", user_id=7, chat_id=42, notification_bot=bot
        )

        self.assertTrue(response.success)
        handler._get_or_create_stream.assert_awaited_once()
        callback = handler._get_or_create_stream.await_args.args[4]
        self.assertIs(callback, state.unsolicited_callback)
        bot.send_message.assert_awaited_once_with(
            chat_id=42,
            text="late task done",
        )


class _DisconnectClient:
    def __init__(self):
        self.disconnected = False

    async def disconnect(self):
        self.disconnected = True


class ClearUserStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_clear_cancels_futures_and_disconnects(self):
        handler = project_chat.ProjectChatHandler()
        client = _DisconnectClient()
        state = project_chat._UserStreamState(client=client, model=None)
        req = _make_request(None)
        state.pending.append(req)
        handler._streams[7] = state

        await handler.clear_user_stream(7)

        self.assertTrue(req.future.cancelled())   # revert cancellation semantics kept
        self.assertTrue(client.disconnected)       # SDK subprocess actually torn down
        self.assertNotIn(7, handler._streams)      # stream removed

    async def test_clear_missing_user_is_noop(self):
        handler = project_chat.ProjectChatHandler()
        await handler.clear_user_stream(999)  # must not raise


class _CapturingClient:
    """Client that records the in-flight request's streaming_handler at query time
    and immediately resolves the future so process_message returns."""

    def __init__(self, state):
        self.state = state
        self.captured_handler = "unset"
        self.captured_interim_callback = "unset"

    async def query(self, *_args, **_kwargs):
        req = self.state.pending[0]
        self.captured_handler = req.streaming_handler
        self.captured_interim_callback = req.interim_message_callback
        req.future.set_result(
            project_chat.ChatResponse(content="ok", session_id="s")
        )


class StreamingGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_streaming_handler_when_disabled(self):
        # Even with a bot passed, streaming OFF means no live draft handler is
        # created — the reply is delivered as a complete message by the caller.
        handler = project_chat.ProjectChatHandler()
        state = project_chat._UserStreamState(client=None, model=None)
        client = _CapturingClient(state)
        state.client = client

        async def fake_goc(*_a, **_k):
            return state

        handler._get_or_create_stream = fake_goc
        config_module.config.enable_streaming = False
        try:
            resp = await handler.process_message(
                "hi", user_id=7, chat_id=42, bot=object()
            )
        finally:
            config_module.config.enable_streaming = None
        self.assertIsNone(client.captured_handler)
        self.assertEqual(resp.content, "ok")

    async def test_process_message_wires_interim_callback_without_streaming(self):
        handler = project_chat.ProjectChatHandler()
        state = project_chat._UserStreamState(client=None, model=None)
        client = _CapturingClient(state)
        state.client = client

        async def fake_goc(*_a, **_k):
            return state

        async def deliver_interim(_content):
            return None

        handler._get_or_create_stream = fake_goc
        config_module.config.enable_streaming = False
        try:
            await handler.process_message(
                "hi",
                user_id=7,
                chat_id=42,
                interim_message_callback=deliver_interim,
            )
        finally:
            config_module.config.enable_streaming = None
        self.assertIs(client.captured_interim_callback, deliver_interim)


class ChatScopedStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_user_different_chats_get_separate_streams(self):
        handler = project_chat.ProjectChatHandler()
        created = []

        async def fake_create(
            user_id, model, unsolicited_callback=None, *, chat_id=None
        ):
            state = project_chat._UserStreamState(
                client=object(),
                model=model,
                unsolicited_callback=unsolicited_callback,
            )
            created.append((user_id, chat_id, state))
            return state

        handler._create_user_stream = fake_create

        private_state = await handler._get_or_create_stream(7, 7, None, False)
        group_state = await handler._get_or_create_stream(7, -10042, None, False)
        private_again = await handler._get_or_create_stream(7, 7, None, False)

        self.assertIs(private_again, private_state)
        self.assertIsNot(private_state, group_state)
        self.assertIn((7, 7), handler._streams)
        self.assertIn((7, -10042), handler._streams)
        self.assertEqual(len(created), 2)
        self.assertEqual(
            [(row[0], row[1]) for row in created], [(7, 7), (7, -10042)]
        )


if __name__ == "__main__":
    unittest.main()
