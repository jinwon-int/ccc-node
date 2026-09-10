import asyncio
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

from sys_modules_isolation import ModuleFakesGuard

os.environ.setdefault("PROJECT_ROOT", str(Path(__file__).resolve().parents[1]))
BRIDGE_DIR = Path(__file__).resolve().parents[1]

_sys_modules_guard = ModuleFakesGuard(__name__).begin()

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
sdk_module.HookMatcher = type("HookMatcher", (), {"__init__": lambda self, **kwargs: None})
sdk_module.AssistantMessage = type("AssistantMessage", (), {})
sdk_module.RateLimitEvent = type("RateLimitEvent", (), {})
sdk_module.ResultMessage = type("ResultMessage", (), {})
sdk_module.StreamEvent = type("StreamEvent", (), {})
sdk_module.TextBlock = type("TextBlock", (), {})
sdk_module.ToolUseBlock = type("ToolUseBlock", (), {})
sdk_module.PermissionResultAllow = _PermissionResultAllow
sdk_module.PermissionResultDeny = _PermissionResultDeny
sdk_module.__path__ = []
sys.modules.setdefault("claude_agent_sdk", sdk_module)

# Complete the stub set: tool_policy imports
# claude_agent_sdk.types, so a solo run of this module must not depend on a
# sibling test having imported the real SDK first.
sdk_types_module = types.ModuleType("claude_agent_sdk.types")
sdk_types_module.HookContext = type("HookContext", (), {})
sdk_types_module.HookInput = type("HookInput", (), {})
sdk_types_module.HookJSONOutput = type("HookJSONOutput", (), {})
sdk_types_module.PermissionResultAllow = _PermissionResultAllow
sdk_types_module.PermissionResultDeny = _PermissionResultDeny
sys.modules.setdefault("claude_agent_sdk.types", sdk_types_module)

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

_sys_modules_guard.finish()


async def _wait_until(predicate, *, timeout: float = 5.0) -> None:
    """Bounded poll for a condition that lands off the event loop (#1537).

    Ledger writes are offloaded to a worker thread (#1479), so a single
    ``asyncio.sleep(0)`` tick does not guarantee the write has landed on a
    loaded runner. Same 5s completion cap as the #1516 waits.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition was not reached in time")
        await asyncio.sleep(0.01)


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

    async def _start_loop(self, req):
        task = asyncio.create_task(self.handler._agent_progress_loop(req))
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
        await self._start_loop(req)
        await asyncio.wait_for(self.status_event.wait(), timeout=5.0)
        self.assertEqual(req.heartbeat_message_id, 1234)
        self.assertIn("⏳ Working", self.status_calls[0][0])
        self.assertIn("Read: bridge/core/project_chat.py", self.status_calls[0][0])

    async def test_does_not_send_when_disabled(self):
        project_chat.config.heartbeat_enabled = False
        self.addCleanup(setattr, project_chat.config, "heartbeat_enabled", True)
        req = self._make_request()
        await self._start_loop(req)
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(self.status_event.wait(), timeout=0.1)
        self.assertEqual(self.status_calls, [])

    async def test_terminal_request_suppresses_heartbeat_before_future_done(self):
        req = self._make_request()
        req.lifecycle.try_terminal(
            project_chat.RequestPhase.TIMEOUT,
            cause="process-timeout",
        )
        await self.handler._maybe_update_heartbeat(
            req,
            asyncio.get_running_loop().time() + 60.0,
        )
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
        await self._start_loop(req)
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
            await self._start_loop(req)
            await asyncio.wait_for(self.status_event.wait(), timeout=5.0)
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
            await self._start_loop(req)
            await asyncio.wait_for(self.status_event.wait(), timeout=5.0)
            text = self.status_calls[0][0]
            self.assertIn("Working", text)
            self.assertNotIn("ETA", text)

    async def test_silent_long_tool_keeps_editing_same_heartbeat_without_eta(self):
        project_chat.config.heartbeat_stall_seconds = 300.0
        self.addCleanup(setattr, project_chat.config, "heartbeat_stall_seconds", 0.0)
        req = self._make_request()
        req.heartbeat_message_id = 1234
        req.current_tool_label = "bash"
        req.heartbeat_forecast_loaded = True
        req.heartbeat_forecast_samples = [2000000, 2400000, 3000000]
        # A fresh CI runner can have less than 301s of monotonic uptime.
        # Use a complete synthetic timeline so prior events stay positive.
        now = 10_000.0
        req.started_at = now - 1200.0
        req.last_event_at = now - 301.0
        await self.handler._maybe_update_heartbeat(req, now)
        await self.handler._maybe_update_heartbeat(req, now + 30.0)
        self.assertEqual(req.heartbeat_message_id, 1234)
        self.assertEqual(len(self.status_calls), 2)
        self.assertIn("20m 00s", self.status_calls[0][0])
        self.assertIn("20m 30s", self.status_calls[1][0])
        for text, message_id in self.status_calls:
            self.assertEqual(message_id, 1234)
            self.assertIn("Waiting for progress", text)
            self.assertIn("Last: bash", text)
            self.assertNotIn("ETA", text)

    async def test_silent_update_failure_retries_without_deleting(self):
        project_chat.config.heartbeat_stall_seconds = 0.05
        self.addCleanup(setattr, project_chat.config, "heartbeat_stall_seconds", 0.0)

        async def failing_status_callback(text, message_id=None):
            self.status_calls.append((text, message_id))
            raise OSError("offline fixture")

        req = self._make_request()
        req.status_callback = failing_status_callback
        req.heartbeat_message_id = 1234
        now = asyncio.get_running_loop().time()
        req.started_at = now - 10.0
        req.last_event_at = now - 10.0
        await self.handler._maybe_update_heartbeat(req, now)
        await self.handler._maybe_update_heartbeat(req, now + 1.0)
        self.assertEqual(len(self.status_calls), 2)
        self.assertTrue(all(text is not None for text, _ in self.status_calls))
        self.assertEqual(req.heartbeat_message_id, 1234)

    async def test_no_first_event_recreates_previously_missing_heartbeat(self):
        project_chat.config.heartbeat_stall_seconds = 0.05
        self.addCleanup(setattr, project_chat.config, "heartbeat_stall_seconds", 0.0)
        req = self._make_request()
        now = asyncio.get_running_loop().time()
        req.started_at = now - 10.0
        req.last_event_at = 0.0
        await self.handler._maybe_update_heartbeat(req, now)
        self.assertIn("Waiting for progress", self.status_calls[0][0])
        self.assertEqual(req.heartbeat_message_id, 1234)

    async def test_fresh_event_restores_working_and_terminal_cleanup_still_deletes(self):
        project_chat.config.heartbeat_stall_seconds = 0.05
        self.addCleanup(setattr, project_chat.config, "heartbeat_stall_seconds", 0.0)
        req = self._make_request()
        now = asyncio.get_running_loop().time()
        req.started_at = now - 10.0
        req.last_event_at = now - 10.0
        await self.handler._maybe_update_heartbeat(req, now)
        self.assertIn("Waiting for progress", self.status_calls[-1][0])
        req.last_event_at = now + 1.0
        await self.handler._maybe_update_heartbeat(req, now + 1.0)
        self.assertIn("Working", self.status_calls[-1][0])
        self.assertNotIn("No update", self.status_calls[-1][0])
        req.future.set_result(None)
        count = len(self.status_calls)
        await self.handler._maybe_update_heartbeat(req, now + 2.0)
        self.assertEqual(len(self.status_calls), count)
        self.assertTrue(await self.handler._cleanup_heartbeat(req))
        self.assertEqual(self.status_calls[-1], (None, 1234))

    async def test_silent_and_active_conversations_keep_separate_status(self):
        project_chat.config.heartbeat_stall_seconds = 300.0
        self.addCleanup(setattr, project_chat.config, "heartbeat_stall_seconds", 0.0)
        quiet, active = self._make_request(), self._make_request()
        quiet.chat_id, active.chat_id = 10, 20
        quiet.heartbeat_message_id, active.heartbeat_message_id = 100, 200
        # A fresh CI runner can have less than 301s of monotonic uptime.
        # Use a complete synthetic timeline so prior events stay positive.
        now = 10_000.0
        quiet.started_at = active.started_at = now - 1200.0
        quiet.last_event_at, active.last_event_at = now - 301.0, now
        await asyncio.gather(self.handler._maybe_update_heartbeat(quiet, now),
                             self.handler._maybe_update_heartbeat(active, now))
        calls = {message_id: text for text, message_id in self.status_calls}
        self.assertIn("Waiting for progress", calls[100])
        self.assertIn("Working", calls[200])
        quiet.future.set_result(None)
        await self.handler._cleanup_heartbeat(quiet)
        self.assertEqual(active.heartbeat_message_id, 200)

    async def test_silence_indicator_can_be_disabled_without_removing_status(self):
        project_chat.config.heartbeat_stall_seconds = 0.0
        req = self._make_request()
        now = asyncio.get_running_loop().time()
        req.started_at = now - 1000.0
        await self.handler._maybe_update_heartbeat(req, now)
        self.assertIn("Working", self.status_calls[-1][0])
        self.assertNotIn("No update", self.status_calls[-1][0])

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
        self.handler._agent_session_registry.register_active(
            (1, 2),
            object(),
            started_at=now - 30,
        )
        self.handler._agent_session_registry.register_active(
            (3, 4),
            object(),
            started_at=now - 10,
        )
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
            req.task_id = await self.handler._ledger_create(1, 2)
            await self._start_loop(req)
            await asyncio.wait_for(self.status_event.wait(), timeout=5.0)
            led = self.handler._task_ledger
            # The registration write runs in a worker thread after the status
            # callback returns; poll until it lands instead of one loop tick.
            await _wait_until(
                lambda: len(led.records()) == 1
                and led.records()[0].get("status_message_id") == 1234
            )
            records = led.records()
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
            req.task_id = await self.handler._ledger_create(1, 2)
            self.handler._task_ledger.set_status_message(req.task_id, 999)
            req.heartbeat_message_id = 999
            cleaned = await self.handler._cleanup_heartbeat(req)
            self.assertFalse(cleaned)
            await self.handler._ledger_finish(req, "completed", cleanup_done=cleaned)
            ops = self.handler._task_ledger.pending_terminal_ops()
            self.assertEqual(len(ops), 1)
            self.assertEqual(ops[0][1]["message_id"], 999)

    async def test_successful_cleanup_purges_ledger_record_on_finish(self):
        with tempfile.TemporaryDirectory() as td:
            project_chat.config.bot_data_dir = Path(td)
            self.addCleanup(delattr, project_chat.config, "bot_data_dir")
            self.handler._task_ledger_cache = None
            req = self._make_request()
            req.task_id = await self.handler._ledger_create(1, 2)
            self.handler._task_ledger.set_status_message(req.task_id, 555)
            req.heartbeat_message_id = 555
            cleaned = await self.handler._cleanup_heartbeat(req)
            self.assertTrue(cleaned)
            await self.handler._ledger_finish(req, "completed", cleanup_done=cleaned)
            self.assertEqual(self.handler._task_ledger.records(), [])

    async def test_ledger_verbs_run_off_the_event_loop_thread(self):
        """#1479: create/phase/finish are fsync-backed and must not block the loop."""
        with tempfile.TemporaryDirectory() as td:
            project_chat.config.bot_data_dir = Path(td)
            self.addCleanup(delattr, project_chat.config, "bot_data_dir")
            self.handler._task_ledger_cache = None
            led = self.handler._task_ledger
            loop_thread = threading.get_ident()
            off_loop: dict[str, bool] = {}
            for name in ("create", "set_state", "finish"):
                original = getattr(led, name)

                def wrapper(*args, _name=name, _orig=original, **kwargs):
                    off_loop[_name] = threading.get_ident() != loop_thread
                    return _orig(*args, **kwargs)

                setattr(led, name, wrapper)

            req = self._make_request()
            req.task_id = await self.handler._ledger_create(1, 2)
            self.assertIsNotNone(req.task_id)
            self.assertTrue(req.lifecycle.admit())
            await self.handler._project_request_phase(req)
            self.assertEqual(led.records()[0]["state"], "working")
            req.lifecycle.try_terminal(
                project_chat.RequestPhase.COMPLETED, cause="normal-completion"
            )
            await self.handler._ledger_finish(req, "completed", cleanup_done=True)
            self.assertEqual(led.records(), [])
            self.assertEqual(
                off_loop, {"create": True, "set_state": True, "finish": True}
            )

    async def test_concurrent_phase_projections_land_in_issue_order(self):
        """#1479: a slow earlier projection must not overwrite a newer phase."""
        with tempfile.TemporaryDirectory() as td:
            project_chat.config.bot_data_dir = Path(td)
            self.addCleanup(delattr, project_chat.config, "bot_data_dir")
            self.handler._task_ledger_cache = None
            led = self.handler._task_ledger
            original_set_state = led.set_state
            calls: list[str] = []

            def slow_working_set_state(task_id, state):
                if state == "working":
                    time.sleep(0.05)  # outside the ledger lock: only the caller waits
                calls.append(state)
                return original_set_state(task_id, state)

            led.set_state = slow_working_set_state
            req = self._make_request()
            req.task_id = await self.handler._ledger_create(1, 2)
            self.assertTrue(req.lifecycle.admit())
            first = asyncio.create_task(self.handler._project_request_phase(req))
            await asyncio.sleep(0)  # first snapshots "working" and enters the write
            self.assertIsNotNone(req.lifecycle.begin_approval())
            second = asyncio.create_task(self.handler._project_request_phase(req))
            await asyncio.gather(first, second)
            self.assertEqual(calls, ["working", "input-required"])
            self.assertEqual(led.records()[0]["state"], "input-required")

    async def test_ledger_projection_failures_do_not_change_lifecycle(self):
        class ExplodingLedger:
            def set_state(self, *args, **kwargs):
                raise OSError("projection unavailable")

            def finish(self, *args, **kwargs):
                raise OSError("finish unavailable")

        req = self._make_request()
        req.task_id = "task"
        self.handler._task_ledger_cache = ExplodingLedger()
        self.addCleanup(setattr, self.handler, "_task_ledger_cache", False)

        self.assertTrue(req.lifecycle.admit())
        with self.assertLogs(project_chat.logger, level="WARNING"):
            await self.handler._project_request_phase(req)
        self.assertEqual(req.lifecycle.phase, project_chat.RequestPhase.WORKING)

        req.lifecycle.try_terminal(
            project_chat.RequestPhase.COMPLETED,
            cause="normal-completion",
        )
        with self.assertLogs(project_chat.logger, level="WARNING"):
            await self.handler._ledger_finish(req, "completed", cleanup_done=True)
        self.assertEqual(req.lifecycle.phase, project_chat.RequestPhase.COMPLETED)

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
            await self._start_loop(req)
            await asyncio.wait_for(self.status_event.wait(), timeout=5.0)
            self.assertIn("ETA ~1m 30s", self.status_calls[0][0])


if __name__ == "__main__":
    unittest.main()
