import asyncio
import os
import sys
import tempfile
import types
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("PROJECT_ROOT", str(Path(__file__).resolve().parents[1]))
BRIDGE_DIR = Path(__file__).resolve().parents[1]

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
subprocess_cli_module = types.ModuleType("claude_agent_sdk._internal.transport.subprocess_cli")
subprocess_cli_module.SubprocessCLITransport = type("SubprocessCLITransport", (), {})
sys.modules.setdefault("claude_agent_sdk._internal", internal_module)
sys.modules.setdefault("claude_agent_sdk._internal.transport", transport_pkg)
sys.modules.setdefault("claude_agent_sdk._internal.transport.subprocess_cli", subprocess_cli_module)

_config_module = types.ModuleType("telegram_bot.utils.config")
_config_module.config = SimpleNamespace(
    claude_cli_path=None,
    heartbeat_enabled=True,
    heartbeat_threshold_seconds=0.02,
    heartbeat_update_interval_seconds=0.02,
    heartbeat_suppress_when_streaming_progress=True,
    heartbeat_delete_on_done=True,
    heartbeat_duration_log_path=None,
    heartbeat_forecast_enabled=False,
    heartbeat_forecast_min_samples=10,
)
sys.modules["telegram_bot.utils.config"] = _config_module

_chat_logger_module = types.ModuleType("telegram_bot.utils.chat_logger")
_chat_logger_module.log_chat = lambda *args, **kwargs: None
sys.modules["telegram_bot.utils.chat_logger"] = _chat_logger_module

_health_module = types.ModuleType("telegram_bot.utils.health")
_health_module.health_reporter = SimpleNamespace(
    record_claude_error=lambda *args, **kwargs: None,
    record_claude_ok=lambda *args, **kwargs: None,
)
sys.modules["telegram_bot.utils.health"] = _health_module

import importlib  # noqa: E402

sys.modules.pop("telegram_bot.core.project_chat", None)
project_chat = importlib.import_module("telegram_bot.core.project_chat")
ProjectChatHandler = project_chat.ProjectChatHandler
_PendingRequest = project_chat._PendingRequest
_UserStreamState = project_chat._UserStreamState


class HeartbeatLoopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._orig_interval = project_chat.TYPING_INTERVAL
        project_chat.TYPING_INTERVAL = 0.01
        self.addCleanup(setattr, project_chat, "TYPING_INTERVAL", self._orig_interval)
        self.handler = ProjectChatHandler()
        self.status_calls = []
        self.status_event = asyncio.Event()

    async def _status_callback(self, text, message_id=None):
        self.status_calls.append((text, message_id))
        self.status_event.set()
        if text is None:
            return None
        return message_id or 1234

    def _make_request(self, *, done=False, streaming_handler=None):
        future = asyncio.get_running_loop().create_future()
        req = _PendingRequest(
            user_id=1,
            chat_id=2,
            model=None,
            requested_session_id=None,
            permission_callback=None,
            typing_callback=None,
            future=future,
            status_callback=self._status_callback,
            streaming_handler=streaming_handler,
        )
        req.started_at = asyncio.get_running_loop().time()
        req.current_tool_label = "Read: bridge/core/project_chat.py"
        if done:
            future.set_result("done")
        return req

    async def _start_loop(self, state):
        task = asyncio.create_task(self.handler._typing_keepalive_loop(1, state))
        self.addCleanup(self._cancel, task)
        return task

    @staticmethod
    async def _cancel(task):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def test_sends_heartbeat_after_threshold_without_typing_callback(self):
        req = self._make_request()
        state = _UserStreamState(client=None, model=None, pending=deque([req]))
        await self._start_loop(state)
        await asyncio.wait_for(self.status_event.wait(), timeout=1.0)
        self.assertEqual(req.heartbeat_message_id, 1234)
        self.assertIn("⏳ Working", self.status_calls[0][0])
        self.assertIn("Read: bridge/core/project_chat.py", self.status_calls[0][0])

    async def test_does_not_send_when_disabled(self):
        project_chat.config.heartbeat_enabled = False
        self.addCleanup(setattr, project_chat.config, "heartbeat_enabled", True)
        req = self._make_request()
        state = _UserStreamState(client=None, model=None, pending=deque([req]))
        await self._start_loop(state)
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(self.status_event.wait(), timeout=0.1)
        self.assertEqual(self.status_calls, [])

    async def test_cleanup_deletes_existing_heartbeat(self):
        req = self._make_request()
        req.heartbeat_message_id = 1234
        await self.handler._cleanup_heartbeat(req)
        self.assertEqual(self.status_calls, [(None, 1234)])
        self.assertIsNone(req.heartbeat_message_id)

    async def test_suppresses_when_streaming_recently_showed_progress(self):
        project_chat.config.heartbeat_threshold_seconds = 1.0
        self.addCleanup(setattr, project_chat.config, "heartbeat_threshold_seconds", 0.02)
        streaming_handler = SimpleNamespace(drafts=[SimpleNamespace(message_id=99)])
        req = self._make_request(streaming_handler=streaming_handler)
        now = asyncio.get_running_loop().time()
        req.started_at = now - 2.0
        req.last_visible_progress_at = now
        state = _UserStreamState(client=None, model=None, pending=deque([req]))
        await self._start_loop(state)
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(self.status_event.wait(), timeout=0.1)
        self.assertEqual(self.status_calls, [])

    async def test_includes_forecast_when_enough_duration_samples_exist(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "duration.jsonl"
            path.write_text(
                "".join(
                    '{"user_id":1,"chat_id":2,"model":null,"duration_ms":120000,"success":true}\n'
                    for _ in range(3)
                ),
                encoding="utf-8",
            )
            project_chat.config.heartbeat_forecast_enabled = True
            project_chat.config.heartbeat_forecast_min_samples = 3
            project_chat.config.heartbeat_duration_log_path = path
            self.addCleanup(setattr, project_chat.config, "heartbeat_forecast_enabled", False)
            self.addCleanup(setattr, project_chat.config, "heartbeat_forecast_min_samples", 10)
            self.addCleanup(setattr, project_chat.config, "heartbeat_duration_log_path", None)

            req = self._make_request()
            state = _UserStreamState(client=None, model=None, pending=deque([req]))
            await self._start_loop(state)
            await asyncio.wait_for(self.status_event.wait(), timeout=1.0)
            self.assertIn("ETA ~2m 00s", self.status_calls[0][0])

    async def test_hides_forecast_once_elapsed_exceeds_all_samples(self):
        # Remaining-time ETA conditions on samples longer than elapsed; when a
        # task outlives its whole history the ETA disappears instead of showing
        # a stale "ETA ~2m" under an elapsed of 5m.
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "duration.jsonl"
            path.write_text(
                "".join(
                    '{"user_id":1,"chat_id":2,"model":null,"duration_ms":120000,"success":true}\n'
                    for _ in range(3)
                ),
                encoding="utf-8",
            )
            project_chat.config.heartbeat_forecast_enabled = True
            project_chat.config.heartbeat_forecast_min_samples = 3
            project_chat.config.heartbeat_duration_log_path = path
            self.addCleanup(setattr, project_chat.config, "heartbeat_forecast_enabled", False)
            self.addCleanup(setattr, project_chat.config, "heartbeat_forecast_min_samples", 10)
            self.addCleanup(setattr, project_chat.config, "heartbeat_duration_log_path", None)

            req = self._make_request()
            req.started_at = asyncio.get_running_loop().time() - 300.0  # elapsed 5m
            state = _UserStreamState(client=None, model=None, pending=deque([req]))
            await self._start_loop(state)
            await asyncio.wait_for(self.status_event.wait(), timeout=1.0)
            text = self.status_calls[0][0]
            self.assertIn("Working", text)
            self.assertNotIn("ETA", text)

    async def test_deletes_heartbeat_when_stream_stalls(self):
        project_chat.config.heartbeat_stall_seconds = 0.05
        self.addCleanup(setattr, project_chat.config, "heartbeat_stall_seconds", 0.0)
        req = self._make_request()
        req.heartbeat_message_id = 1234
        now = asyncio.get_running_loop().time()
        req.started_at = now - 10.0
        req.last_event_at = now - 10.0  # silent far longer than the stall window
        await self.handler._maybe_update_heartbeat(req, now)
        self.assertEqual(self.status_calls, [(None, 1234)])
        self.assertIsNone(req.heartbeat_message_id)

    async def test_stall_falls_back_to_started_at_when_no_event_yet(self):
        project_chat.config.heartbeat_stall_seconds = 0.05
        self.addCleanup(setattr, project_chat.config, "heartbeat_stall_seconds", 0.0)
        req = self._make_request()
        req.heartbeat_message_id = 1234
        now = asyncio.get_running_loop().time()
        req.started_at = now - 10.0
        req.last_event_at = 0.0  # the SDK never emitted a single event
        await self.handler._maybe_update_heartbeat(req, now)
        self.assertEqual(self.status_calls, [(None, 1234)])
        self.assertIsNone(req.heartbeat_message_id)

    async def test_recent_activity_keeps_heartbeat(self):
        project_chat.config.heartbeat_stall_seconds = 100.0
        self.addCleanup(setattr, project_chat.config, "heartbeat_stall_seconds", 0.0)
        req = self._make_request()
        now = asyncio.get_running_loop().time()
        req.started_at = now - 10.0
        req.last_event_at = now  # a fresh SDK event just arrived
        await self.handler._maybe_update_heartbeat(req, now)
        self.assertTrue(self.status_calls)
        # A live heartbeat is an edit/send (text present), not a deletion.
        self.assertIsNotNone(self.status_calls[0][0])
        self.assertIn("⏳ Working", self.status_calls[0][0])

    async def test_workload_snapshot_counts_inflight_and_oldest(self):
        now = asyncio.get_running_loop().time()
        r1 = self._make_request()
        r1.started_at = now - 30
        r2 = self._make_request()
        r2.started_at = now - 10
        finished = self._make_request(done=True)  # resolved → not in-flight
        finished.started_at = now - 100
        state = _UserStreamState(
            client=None, model=None, pending=deque([r1, r2, finished])
        )
        self.handler._streams[(1, 2)] = state
        count, oldest = self.handler.workload_snapshot(now)
        self.assertEqual(count, 2)
        self.assertGreaterEqual(oldest, 29.0)
        self.assertLess(oldest, 31.0)

    async def test_workload_snapshot_empty_when_idle(self):
        now = asyncio.get_running_loop().time()
        self.assertEqual(self.handler.workload_snapshot(now), (0, 0.0))

    async def test_heartbeat_send_registers_message_in_task_ledger(self):
        with tempfile.TemporaryDirectory() as td:
            project_chat.config.bot_data_dir = Path(td)
            self.addCleanup(delattr, project_chat.config, "bot_data_dir")
            self.handler._task_ledger_cache = None
            req = self._make_request()
            req.task_id = self.handler._ledger_create(1, 2)
            state = _UserStreamState(client=None, model=None, pending=deque([req]))
            await self._start_loop(state)
            await asyncio.wait_for(self.status_event.wait(), timeout=1.0)
            await asyncio.sleep(0)  # let the registration write land
            records = self.handler._task_ledger.records()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["status_message_id"], 1234)
            self.assertEqual(records[0]["state"], "working")

    async def test_failed_cleanup_leaves_retryable_terminal_op(self):
        with tempfile.TemporaryDirectory() as td:
            project_chat.config.bot_data_dir = Path(td)
            self.addCleanup(delattr, project_chat.config, "bot_data_dir")
            self.handler._task_ledger_cache = None

            async def failing_status_callback(text, message_id=None):
                # Delete swallowed a network error: contract returns message_id.
                return message_id

            req = self._make_request()
            req.status_callback = failing_status_callback
            req.task_id = self.handler._ledger_create(1, 2)
            self.handler._task_ledger.set_status_message(req.task_id, 999)
            req.heartbeat_message_id = 999
            cleaned = await self.handler._cleanup_heartbeat(req)
            self.assertFalse(cleaned)
            self.handler._ledger_finish(req, "completed", cleanup_done=cleaned)
            ops = self.handler._task_ledger.pending_terminal_ops()
            self.assertEqual(len(ops), 1)
            self.assertEqual(ops[0][1]["message_id"], 999)

    async def test_successful_cleanup_purges_ledger_record_on_finish(self):
        with tempfile.TemporaryDirectory() as td:
            project_chat.config.bot_data_dir = Path(td)
            self.addCleanup(delattr, project_chat.config, "bot_data_dir")
            self.handler._task_ledger_cache = None
            req = self._make_request()
            req.task_id = self.handler._ledger_create(1, 2)
            self.handler._task_ledger.set_status_message(req.task_id, 555)
            req.heartbeat_message_id = 555
            cleaned = await self.handler._cleanup_heartbeat(req)
            self.assertTrue(cleaned)
            self.handler._ledger_finish(req, "completed", cleanup_done=cleaned)
            self.assertEqual(self.handler._task_ledger.records(), [])

    async def test_forecast_shrinks_as_task_progresses(self):
        # Same history, elapsed 30s -> remaining should be ~1m 30s, not the
        # full 2m total-median the old fixed forecast displayed.
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "duration.jsonl"
            path.write_text(
                "".join(
                    '{"user_id":1,"chat_id":2,"model":null,"duration_ms":120000,"success":true}\n'
                    for _ in range(3)
                ),
                encoding="utf-8",
            )
            project_chat.config.heartbeat_forecast_enabled = True
            project_chat.config.heartbeat_forecast_min_samples = 3
            project_chat.config.heartbeat_duration_log_path = path
            self.addCleanup(setattr, project_chat.config, "heartbeat_forecast_enabled", False)
            self.addCleanup(setattr, project_chat.config, "heartbeat_forecast_min_samples", 10)
            self.addCleanup(setattr, project_chat.config, "heartbeat_duration_log_path", None)

            req = self._make_request()
            req.started_at = asyncio.get_running_loop().time() - 30.0
            state = _UserStreamState(client=None, model=None, pending=deque([req]))
            await self._start_loop(state)
            await asyncio.wait_for(self.status_event.wait(), timeout=1.0)
            self.assertIn("ETA ~1m 30s", self.status_calls[0][0])


if __name__ == "__main__":
    unittest.main()
