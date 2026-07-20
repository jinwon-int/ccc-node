"""Terminal-event stall release for the Claude SDK path (#411 C).

A turn that produced assistant text but whose terminal ResultMessage never
arrives previously held the conversation FIFO until the full process timeout
(default 21600s). These tests pin the bounded release: the buffered answer is
delivered exactly once with a stall notice, the request terminalizes, and a
late ResultMessage racing the teardown is swallowed instead of double-
delivered through the unsolicited route.
"""

import asyncio
import os
import sys
import types
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

# project_chat reads config / PROJECT_ROOT / health / chat_logger at import
# time; install minimal fakes and re-import fresh (the defensive setup used by
# test_typing_keepalive / test_project_chat_retry).
os.environ.setdefault("PROJECT_ROOT", str(Path(__file__).resolve().parents[1]))

from sys_modules_isolation import ModuleFakesGuard  # noqa: E402

_sys_modules_guard = ModuleFakesGuard(__name__).begin()

_config_module = types.ModuleType("telegram_bot.utils.config")
_config_module.config = SimpleNamespace(claude_cli_path=None, terminal_stall_seconds=0.05)
sys.modules["telegram_bot.utils.config"] = _config_module

_chat_logger_module = types.ModuleType("telegram_bot.utils.chat_logger")
_chat_logger_module.log_chat = lambda *args, **kwargs: None
sys.modules["telegram_bot.utils.chat_logger"] = _chat_logger_module

_STALLED_COUNTS: list[int] = []
_health_module = types.ModuleType("telegram_bot.utils.health")
_health_module.health_reporter = SimpleNamespace(
    record_claude_error=lambda *args, **kwargs: None,
    record_claude_ok=lambda *args, **kwargs: None,
    record_stalled_request=lambda count=1: _STALLED_COUNTS.append(count),
)
sys.modules["telegram_bot.utils.health"] = _health_module

import importlib  # noqa: E402

# Re-import the reader mixin alongside project_chat so both bind the SAME
# claude_agent_sdk generation — sibling test modules swap it for stubs, and a
# cross-generation mix would break the reader's isinstance checks.
sys.modules.pop("telegram_bot.core.project_chat_reader", None)
sys.modules.pop("telegram_bot.core.project_chat", None)
project_chat = importlib.import_module("telegram_bot.core.project_chat")
ProjectChatHandler = project_chat.ProjectChatHandler
_PendingRequest = project_chat._PendingRequest
_UserStreamState = project_chat._UserStreamState

_sys_modules_guard.finish()


def _result_message(result="late terminal frame"):
    fields = dict(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="s1",
        result=result,
    )
    try:
        return project_chat.ResultMessage(**fields)
    except TypeError:
        # A sibling test module replaced claude_agent_sdk with attribute-less
        # stubs; instantiate the stub and attach the fields it would carry.
        message = project_chat.ResultMessage.__new__(project_chat.ResultMessage)
        for key, value in fields.items():
            setattr(message, key, value)
        return message


class TerminalStallReleaseTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._orig_interval = project_chat.TYPING_INTERVAL
        project_chat.TYPING_INTERVAL = 0.01
        self.addCleanup(setattr, project_chat, "TYPING_INTERVAL", self._orig_interval)
        project_chat.config.terminal_stall_seconds = 0.05
        _STALLED_COUNTS.clear()
        self.handler = ProjectChatHandler()
        self.handler._task_ledger_cache = False
        self.handler._disconnect_user_stream = AsyncMock()

    def _stalled_request(
        self,
        *,
        texts=("the buffered answer",),
        text_age=1.0,
        tool_age=None,
        event_age=10.0,
        awaiting_permission=False,
    ) -> _PendingRequest:
        now = asyncio.get_event_loop().time()
        req = _PendingRequest(
            user_id=1,
            chat_id=2,
            model=None,
            requested_session_id="s1",
            permission_callback=None,
            typing_callback=None,
            future=asyncio.get_event_loop().create_future(),
        )
        req.started_at = now - event_age
        req.last_event_at = now - event_age
        req.last_assistant_texts = list(texts)
        req.last_text_at = now - text_age if texts else 0.0
        req.last_tool_at = now - tool_age if tool_age is not None else 0.0
        req.awaiting_permission = awaiting_permission
        return req

    async def _run_loop(self, state) -> asyncio.Task:
        task = asyncio.create_task(self.handler._typing_keepalive_loop(1, state))

        async def cancel():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        self.addAsyncCleanup(cancel)
        return task

    async def test_stall_release_delivers_buffered_text_and_frees_fifo(self):
        req = self._stalled_request()
        state = _UserStreamState(client=None, model=None, pending=deque([req]))
        state.last_session_id = "s1"
        await self._run_loop(state)

        response = await asyncio.wait_for(req.future, timeout=2.0)

        self.assertTrue(response.success)
        self.assertIn("the buffered answer", response.content)
        self.assertIn("closed automatically", response.content)
        self.assertEqual(response.session_id, "s1")
        # The FIFO head is released so a queued request can proceed.
        self.assertEqual(len(state.pending), 0)
        self.assertTrue(state.stall_swallow_result)
        self.assertEqual(_STALLED_COUNTS, [1])
        # The dead stream is torn down from outside the keepalive task.
        await asyncio.sleep(0)
        self.handler._disconnect_user_stream.assert_awaited_once_with(1, 2)

    async def test_late_result_message_is_swallowed_exactly_once(self):
        req = self._stalled_request()
        state = _UserStreamState(client=None, model=None, pending=deque([req]))
        delivered = []

        async def unsolicited(content, session_id):
            delivered.append(content)

        state.unsolicited_callback = unsolicited
        await self._run_loop(state)
        await asyncio.wait_for(req.future, timeout=2.0)

        # The racing terminal frame of the released turn is swallowed …
        await self.handler._handle_unsolicited_message(1, state, _result_message())
        self.assertEqual(delivered, [])
        self.assertFalse(state.stall_swallow_result)
        # … but only that one: later results take the normal unsolicited route.
        await self.handler._handle_unsolicited_message(
            1, state, _result_message("a real background result")
        )
        self.assertEqual(len(delivered), 1)
        self.assertIn("a real background result", delivered[0])

    async def test_no_release_without_assistant_text(self):
        req = self._stalled_request(texts=())
        state = _UserStreamState(client=None, model=None, pending=deque([req]))
        await self._run_loop(state)

        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(req.future), timeout=0.2)
        self.assertEqual(_STALLED_COUNTS, [])

    async def test_no_release_while_tool_is_latest_activity(self):
        # Tool started after the text: a long silent tool run is normal.
        req = self._stalled_request(text_age=5.0, tool_age=1.0)
        state = _UserStreamState(client=None, model=None, pending=deque([req]))
        await self._run_loop(state)

        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(req.future), timeout=0.2)

    async def test_no_release_while_awaiting_permission(self):
        req = self._stalled_request(awaiting_permission=True)
        state = _UserStreamState(client=None, model=None, pending=deque([req]))
        await self._run_loop(state)

        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(req.future), timeout=0.2)

    async def test_recent_event_resets_the_stall_clock(self):
        # Text was produced long ago, but a fresh SDK event arrived just now:
        # the grace countdown restarts from the last event, not the last text.
        project_chat.config.terminal_stall_seconds = 10.0
        req = self._stalled_request(text_age=60.0, event_age=0.0)
        state = _UserStreamState(client=None, model=None, pending=deque([req]))
        await self._run_loop(state)

        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(req.future), timeout=0.2)

    async def test_no_release_when_disabled(self):
        project_chat.config.terminal_stall_seconds = 0.0
        req = self._stalled_request()
        state = _UserStreamState(client=None, model=None, pending=deque([req]))
        await self._run_loop(state)

        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(req.future), timeout=0.2)


class TeardownTaskTrackingTest(unittest.IsolatedAsyncioTestCase):
    """#447: teardown spawned from outside the loop task must retain a strong
    reference (GC-safe) and surface exceptions via the done-callback instead of
    dropping them silently."""

    async def asyncSetUp(self):
        self.handler = ProjectChatHandler()
        self.handler._task_ledger_cache = False

    async def test_teardown_exception_is_logged_not_swallowed(self):
        async def _boom():
            raise RuntimeError("teardown blew up")

        with self.assertLogs(project_chat.logger, level="ERROR") as cm:
            task = self.handler._spawn_teardown_task(_boom(), label="unit")
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.sleep(0)  # let the done-callback run

        self.assertTrue(
            any("teardown blew up" in line for line in cm.output),
            cm.output,
        )
        # Reference is released after completion (no unbounded growth).
        self.assertNotIn(task, self.handler._teardown_tasks)

    async def test_teardown_reference_retained_until_done(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def _slow():
            started.set()
            await release.wait()

        task = self.handler._spawn_teardown_task(_slow(), label="unit")
        await started.wait()
        # In-flight: the handler holds the only strong reference.
        self.assertIn(task, self.handler._teardown_tasks)
        release.set()
        await task
        await asyncio.sleep(0)
        self.assertNotIn(task, self.handler._teardown_tasks)

    async def test_teardown_cancellation_is_debug_not_error(self):
        release = asyncio.Event()

        async def _slow():
            await release.wait()

        task = self.handler._spawn_teardown_task(_slow(), label="unit")
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
        # Cancellation is expected teardown flow — reference cleaned up.
        self.assertNotIn(task, self.handler._teardown_tasks)


if __name__ == "__main__":
    unittest.main()
