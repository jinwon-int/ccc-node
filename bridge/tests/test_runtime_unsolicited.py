"""Between-turns (unsolicited) frame handling for the Claude runtime adapter.

Assistant output produced outside an active turn — for example the CLI
autonomously continuing after a harness background-task notification — must
be delivered (the retired direct SDK path's unsolicited machinery pinned the
same behavior).  ``ClaudeRuntime`` carries it via the optional
``set_unsolicited_handler`` seam: buffer assistant text between
turns, deliver once on the terminal ResultMessage, keep ownership of an
in-flight autonomous turn when a user turn arrives, and never route mid-turn
frames to the unsolicited handler.

Note on the module name: like ``test_runtime_conformance`` this module drives
the real ``ClaudeRuntime`` over real ``claude_agent_sdk`` frame types, so it
must collect AFTER the project_chat modules that inject spec-less SDK stubs
and after ``test_runtime_conformance``'s purge/re-import — otherwise
``core.claude_runtime`` would bind a different import generation of the SDK
classes than the frames the fakes emit, breaking isinstance routing.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator
import sys
import unittest
import uuid


def _purge_injected_sdk_stubs() -> None:
    """Drop spec-less ``claude_agent_sdk`` stubs left by earlier test modules.

    Same guard as ``test_runtime_conformance``: some project_chat test modules
    inject bare stub modules for ``claude_agent_sdk`` and never restore the
    real package.  This module drives the real ``ClaudeRuntime`` over real SDK
    frame types, so spec-less entries must be evicted before importing.
    """

    for name in [
        module_name
        for module_name in sys.modules
        if module_name == "claude_agent_sdk" or module_name.startswith("claude_agent_sdk.")
    ]:
        if getattr(sys.modules[name], "__spec__", None) is None:
            del sys.modules[name]


_purge_injected_sdk_stubs()

from claude_agent_sdk import (  # noqa: E402 -- must follow the stub purge above
    AssistantMessage,
    ClaudeAgentOptions,
    Message,
    PermissionResultAllow,
    PermissionResultDeny,
    RateLimitEvent,
    RateLimitInfo,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TaskNotificationMessage,
    TaskUpdatedMessage,
    TextBlock,
    ToolPermissionContext,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from telegram_bot.core.agent_runtime import (  # noqa: E402
    AgentEvent,
    ApprovalDecision,
    CompletionEvent,
    ErrorEvent,
    SessionRequest,
    TextDeltaEvent,
)
from telegram_bot.core.claude_runtime import ClaudeRuntime, ClaudeSession  # noqa: E402


class ManualClaudeSdkClient:
    """Fake SDK client with manual frame emission plus scripted turns.

    ``query`` consumes the next queued script: ``"answer"`` (the default)
    emits one assistant+result pair for the turn, ``"hang"`` emits nothing so
    the test controls every subsequent frame by hand.
    """

    def __init__(self, options: ClaudeAgentOptions) -> None:
        self.options = options
        self.session_id = (
            options.resume or options.session_id or "claude-unsolicited-session"
        )
        self._initialized = False
        self.queries: list[str] = []
        self.turn_scripts: deque[str] = deque()
        self.interrupts = 0
        self._messages: asyncio.Queue[Message | None] = asyncio.Queue()
        self.disconnect_gate: asyncio.Event | None = None
        self.disconnect_started = asyncio.Event()
        self.disconnected = asyncio.Event()
        self.disconnect_calls = 0
        self.disconnect_cancellations = 0

    # -- manual frame emission ---------------------------------------------

    def emit(self, message: Message) -> None:
        self._messages.put_nowait(message)

    def emit_stream_delta(self, text: str) -> None:
        self.emit(
            StreamEvent(
                uuid=str(uuid.uuid4()),
                session_id=self.session_id,
                event={
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": text},
                },
            )
        )

    def emit_assistant(self, text: str) -> None:
        self.emit(
            AssistantMessage(
                content=[TextBlock(text=text)],
                model="claude-test-model",
                session_id=self.session_id,
            )
        )

    def emit_result(
        self,
        *,
        result: str | None = None,
        is_error: bool = False,
        usage: dict[str, int] | None = None,
        total_cost_usd: float | None = None,
    ) -> None:
        self.emit(
            ResultMessage(
                subtype="error_during_execution" if is_error else "success",
                duration_ms=5,
                duration_api_ms=3,
                is_error=is_error,
                num_turns=1,
                session_id=self.session_id,
                result=result,
                usage=usage,
                total_cost_usd=total_cost_usd,
            )
        )

    def emit_rate_limit(self, *, utilization: float = 0.5) -> None:
        self.emit(
            RateLimitEvent(
                rate_limit_info=RateLimitInfo(
                    status="allowed_warning",
                    resets_at=1_900_000_000,
                    rate_limit_type="five_hour",
                    utilization=utilization,
                ),
                uuid=str(uuid.uuid4()),
                session_id=self.session_id,
            )
        )

    def pending_frames(self) -> int:
        return self._messages.qsize()

    # -- SdkClient protocol ------------------------------------------------

    async def connect(self) -> None:
        pass

    async def query(self, prompt: str) -> None:
        if not self._initialized:
            self._initialized = True
            self.emit(
                SystemMessage(
                    subtype="init",
                    data={"session_id": self.session_id, "cwd": "/workspace"},
                )
            )
        self.queries.append(prompt)
        script = self.turn_scripts.popleft() if self.turn_scripts else "answer"
        if script == "answer":
            self.emit_assistant("turn answer")
            self.emit_result(result="turn answer")
        elif script == "hang":
            pass  # the test emits this turn's frames (if any) by hand
        elif script == "background":
            tool_id = "toolu-background"
            self.emit(
                AssistantMessage(
                    content=[
                        ToolUseBlock(
                            id=tool_id,
                            name="Bash",
                            input={"command": "validate", "run_in_background": True},
                        )
                    ],
                    model="claude-test-model",
                    session_id=self.session_id,
                )
            )
            self.emit(
                UserMessage(
                    content=[
                        ToolResultBlock(
                            tool_use_id=tool_id,
                            content=[
                                {
                                    "type": "text",
                                    "text": "Command running in background with ID: bg-42",
                                }
                            ],
                            is_error=False,
                        )
                    ]
                )
            )
            self.emit_assistant("validation started")
            self.emit_result(result="validation started")

    async def receive_messages(self) -> AsyncIterator[Message]:
        while True:
            message = await self._messages.get()
            if message is None:
                return
            yield message

    async def interrupt(self) -> None:
        self.interrupts += 1

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.disconnect_started.set()
        try:
            if self.disconnect_gate is not None:
                await self.disconnect_gate.wait()
        except asyncio.CancelledError:
            self.disconnect_cancellations += 1
            raise
        self._messages.put_nowait(None)
        self.disconnected.set()


async def _collect(stream: AsyncIterator[AgentEvent]) -> list[AgentEvent]:
    return [event async for event in stream]


async def _wait_until(predicate, *, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition was not reached in time")
        await asyncio.sleep(0.001)


class ClaudeRuntimeUnsolicitedTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.clients: list[ManualClaudeSdkClient] = []

        def factory(options: ClaudeAgentOptions) -> ManualClaudeSdkClient:
            client = ManualClaudeSdkClient(options)
            self.clients.append(client)
            return client

        self.runtime = ClaudeRuntime(sdk_client_factory=factory)
        self.addAsyncCleanup(self.runtime.close)
        self.delivered: list[tuple[str, str | None]] = []
        self.delivered_event = asyncio.Event()

    async def _handler(self, text: str, session_id: str | None) -> None:
        self.delivered.append((text, session_id))
        self.delivered_event.set()

    async def _start_session(self) -> tuple[ClaudeSession, ManualClaudeSdkClient]:
        session = await self.runtime.start_or_resume(
            SessionRequest(working_directory="/workspace")
        )
        return session, self.clients[-1]

    async def _drain(self, client: ManualClaudeSdkClient) -> None:
        """Wait until the reader task consumed every emitted frame."""

        await _wait_until(lambda: client.pending_frames() == 0)
        # The final frame may still be mid-routing after the queue empties;
        # yield a few times so its (synchronous) routing completes.
        for _ in range(5):
            await asyncio.sleep(0)

    async def test_closed_session_disconnects_and_leaves_runtime_registry(self) -> None:
        session, client = await self._start_session()

        self.assertIn(session, self.runtime._sessions)
        await session.close()

        self.assertEqual(client.disconnect_calls, 1)
        self.assertNotIn(session, self.runtime._sessions)
        await session.close()
        self.assertEqual(client.disconnect_calls, 1)

    # -- between-turns delivery --------------------------------------------

    async def test_between_turns_output_is_delivered_then_turns_still_work(self) -> None:
        session, client = await self._start_session()
        session.set_unsolicited_handler(self._handler)

        # Autonomous assistant+result pair with NO active turn: the text is
        # buffered and delivered once, on the terminal ResultMessage (the
        # result carries no text of its own, proving accumulation).
        client.emit_assistant("background report")
        client.emit_result(result=None)
        await asyncio.wait_for(self.delivered_event.wait(), timeout=2.0)
        self.assertEqual(self.delivered, [("background report", client.session_id)])

        # A normal turn afterwards still streams through send_turn.
        events = await _collect(session.send_turn("hello"))
        texts = [event.text for event in events if isinstance(event, TextDeltaEvent)]
        self.assertEqual(texts, ["turn answer"])
        self.assertIsInstance(events[-1], CompletionEvent)
        self.assertEqual(len(self.delivered), 1)

    async def test_background_bash_is_tracked_until_terminal_notification(self) -> None:
        session, client = await self._start_session()
        client.turn_scripts.append("background")

        await _collect(session.send_turn("run validation"))
        count, oldest = session.background_workload_snapshot(
            asyncio.get_running_loop().time()
        )
        self.assertEqual(count, 1)
        self.assertGreaterEqual(oldest, 0.0)

        client.emit(
            UserMessage(
                content=(
                    "<task-notification><task-id>bg-42</task-id>"
                    "<status>completed</status><summary>green</summary>"
                    "</task-notification>"
                )
            )
        )
        await _wait_until(
            lambda: session.background_workload_snapshot(
                asyncio.get_running_loop().time()
            )[0]
            == 0
        )
        self.assertEqual(
            session.background_workload_snapshot(asyncio.get_running_loop().time()),
            (0, 0.0),
        )

    async def test_background_bash_released_by_typed_task_notification(self) -> None:
        # The live CLI never emits the transcript-XML UserMessage the sibling
        # test exercises — it reports completion as a typed
        # TaskNotificationMessage frame (frame capture, #860). Regression: the
        # tracker must release on that frame or workload_snapshot leaks.
        session, client = await self._start_session()
        client.turn_scripts.append("background")

        await _collect(session.send_turn("run validation"))
        self.assertEqual(
            session.background_workload_snapshot(
                asyncio.get_running_loop().time()
            )[0],
            1,
        )

        client.emit(
            TaskNotificationMessage(
                subtype="task_notification",
                data={"task_id": "bg-42", "status": "completed"},
                task_id="bg-42",
                status="completed",
                output_file="/tmp/bg-42.output",
                summary="green",
                uuid="uuid-bg-42",
                session_id=client.session_id,
            )
        )
        await _wait_until(
            lambda: session.background_workload_snapshot(
                asyncio.get_running_loop().time()
            )[0]
            == 0
        )

    async def test_background_bash_released_by_terminal_task_update(self) -> None:
        session, client = await self._start_session()
        client.turn_scripts.append("background")

        await _collect(session.send_turn("run validation"))

        # A non-terminal update must NOT release the task.
        client.emit(
            TaskUpdatedMessage(
                subtype="task_updated",
                data={"task_id": "bg-42", "patch": {"status": "running"}},
                task_id="bg-42",
                patch={"status": "running"},
            )
        )
        await asyncio.sleep(0)
        self.assertEqual(
            session.background_workload_snapshot(
                asyncio.get_running_loop().time()
            )[0],
            1,
        )

        client.emit(
            TaskUpdatedMessage(
                subtype="task_updated",
                data={"task_id": "bg-42", "patch": {"status": "killed"}},
                task_id="bg-42",
                patch={"status": "killed"},
            )
        )
        await _wait_until(
            lambda: session.background_workload_snapshot(
                asyncio.get_running_loop().time()
            )[0]
            == 0
        )

    async def test_background_roster_reconciles_missed_events(self) -> None:
        # background_tasks_changed carries the authoritative roster: whatever
        # individual frame was missed, the snapshot must self-heal — drop
        # finished ids and adopt unseen ones.
        session, client = await self._start_session()
        client.turn_scripts.append("background")

        await _collect(session.send_turn("run validation"))

        client.emit(
            SystemMessage(
                subtype="background_tasks_changed",
                data={"tasks": [{"task_id": "bg-99", "task_type": "local_bash"}]},
            )
        )
        await _wait_until(
            lambda: session.background_workload_snapshot(
                asyncio.get_running_loop().time()
            )[0]
            == 1
        )

        client.emit(
            SystemMessage(subtype="background_tasks_changed", data={"tasks": []})
        )
        await _wait_until(
            lambda: session.background_workload_snapshot(
                asyncio.get_running_loop().time()
            )[0]
            == 0
        )

    async def test_result_text_wins_over_buffered_assistant_text(self) -> None:
        session, client = await self._start_session()
        session.set_unsolicited_handler(self._handler)

        client.emit_stream_delta("partial ")
        client.emit_assistant("interim text")
        client.emit_result(result="final background text")
        await asyncio.wait_for(self.delivered_event.wait(), timeout=2.0)

        # Mirrors the direct path: ``msg.result or joined assistant texts``,
        # and stream partials are never delivered on the unsolicited route.
        self.assertEqual(
            self.delivered, [("final background text", client.session_id)]
        )

    async def test_mid_turn_frames_are_not_routed_to_the_handler(self) -> None:
        session, client = await self._start_session()
        session.set_unsolicited_handler(self._handler)

        events = await _collect(session.send_turn("hello"))

        texts = [event.text for event in events if isinstance(event, TextDeltaEvent)]
        self.assertEqual(texts, ["turn answer"])
        await self._drain(client)
        self.assertEqual(self.delivered, [])

    async def test_delegated_run_keeps_exact_approval_route_across_intermediate_result(
        self,
    ) -> None:
        """#804: one result may end a model turn while delegated work keeps the run live.

        The locked Agent SDK deliberately keeps its control channel open when
        a local agent/workflow outlives that result.  Reproduce the production
        ordering with many intervening tool cycles, then ask for the identical
        permission again during the delegated continuation.
        """

        session, client = await self._start_session()
        client.turn_scripts.append("hang")
        approvals: list[str] = []

        async def allow(request) -> ApprovalDecision:
            approvals.append(request.request_id)
            return ApprovalDecision.ALLOW

        events_task = asyncio.create_task(
            _collect(session.send_turn("long orchestration", approval_handler=allow))
        )
        await _wait_until(lambda: client.queries == ["long orchestration"])
        client.emit(
            SystemMessage(
                subtype="task_started",
                data={
                    "task_id": "delegated-1",
                    "task_type": "local_agent",
                    "session_id": client.session_id,
                },
            )
        )
        client.emit(
            SystemMessage(
                subtype="task_started",
                data={
                    "task_id": "workflow-1",
                    "task_type": "local_workflow",
                    "session_id": client.session_id,
                },
            )
        )
        await self._drain(client)

        can_use_tool = client.options.can_use_tool
        self.assertIsNotNone(can_use_tool)
        assert can_use_tool is not None
        early = await can_use_tool(
            "Bash",
            {"command": "same-command"},
            ToolPermissionContext(tool_use_id="approval-early", title="run command"),
        )
        self.assertIsInstance(early, PermissionResultAllow)

        # Tool count and payload size are not route lifetimes.  These frames
        # intentionally add pressure without changing the live run owner.
        for index in range(64):
            tool_id = f"toolu-pressure-{index}"
            client.emit(
                AssistantMessage(
                    content=[
                        ToolUseBlock(
                            id=tool_id,
                            name="Read",
                            input={"file_path": f"fixture-{index}"},
                        )
                    ],
                    model="claude-test-model",
                    session_id=client.session_id,
                )
            )
            client.emit(
                UserMessage(
                    content=[
                        ToolResultBlock(
                            tool_use_id=tool_id,
                            content="ok",
                            is_error=False,
                        )
                    ]
                )
            )

        # This result ends the current model turn, not the SDK run: the local
        # agent is still in flight and will wake a continuation turn.
        client.emit_result(result="delegated work still running")
        await self._drain(client)
        late = await can_use_tool(
            "Bash",
            {"command": "same-command"},
            ToolPermissionContext(tool_use_id="approval-late", title="run command"),
        )

        self.assertIsInstance(late, PermissionResultAllow)
        self.assertFalse(events_task.done())
        self.assertEqual(approvals, ["approval-early", "approval-late"])

        client.emit(
            SystemMessage(
                subtype="task_notification",
                data={
                    "task_id": "delegated-1",
                    "status": "completed",
                    "session_id": client.session_id,
                },
            )
        )
        client.emit(
            SystemMessage(
                subtype="task_updated",
                data={
                    "task_id": "workflow-1",
                    "patch": {"status": "completed"},
                    "session_id": client.session_id,
                },
            )
        )
        client.emit_assistant("all delegated work finished")
        client.emit_result(result="all delegated work finished")
        events = await asyncio.wait_for(events_task, timeout=2.0)

        self.assertEqual(
            [event.text for event in events if isinstance(event, TextDeltaEvent)],
            ["all delegated work finished"],
        )
        self.assertIsInstance(events[-1], CompletionEvent)

    async def test_permission_callback_is_not_rebound_across_turn_generation(self) -> None:
        """A callback admitted by turn A must fail closed after turn B takes over."""

        session, client = await self._start_session()
        client.turn_scripts.extend(("hang", "hang"))
        first_started = asyncio.Event()
        release_first = asyncio.Event()

        async def delayed_allow(_request) -> ApprovalDecision:
            first_started.set()
            await release_first.wait()
            return ApprovalDecision.ALLOW

        first_turn = asyncio.create_task(
            _collect(session.send_turn("turn-a", approval_handler=delayed_allow))
        )
        await _wait_until(lambda: client.queries == ["turn-a"])
        can_use_tool = client.options.can_use_tool
        self.assertIsNotNone(can_use_tool)
        assert can_use_tool is not None
        stale_callback = asyncio.create_task(
            can_use_tool(
                "Bash",
                {"command": "turn-a-command"},
                ToolPermissionContext(tool_use_id="turn-a-approval"),
            )
        )
        await asyncio.wait_for(first_started.wait(), timeout=2.0)

        client.emit_result(result="turn a complete")
        await asyncio.wait_for(first_turn, timeout=2.0)

        async def current_allow(_request) -> ApprovalDecision:
            return ApprovalDecision.ALLOW

        second_turn = asyncio.create_task(
            _collect(session.send_turn("turn-b", approval_handler=current_allow))
        )
        await _wait_until(lambda: client.queries == ["turn-a", "turn-b"])
        release_first.set()
        stale = await asyncio.wait_for(stale_callback, timeout=2.0)
        current = await can_use_tool(
            "Bash",
            {"command": "turn-b-command"},
            ToolPermissionContext(tool_use_id="turn-b-approval"),
        )

        self.assertIsInstance(stale, PermissionResultDeny)
        self.assertIsInstance(current, PermissionResultAllow)
        client.emit_result(result="turn b complete")
        await asyncio.wait_for(second_turn, timeout=2.0)

        after_completion = await can_use_tool(
            "Bash",
            {"command": "late-command"},
            ToolPermissionContext(tool_use_id="after-completion"),
        )
        self.assertIsInstance(after_completion, PermissionResultDeny)
        self.assertEqual(
            after_completion.message,
            "No active turn accepts approval requests; start a new user turn and retry",
        )

    async def test_long_lived_task_types_do_not_hold_approval_route_open(self) -> None:
        """Teammates/monitors are not bounded run-completion evidence."""

        session, client = await self._start_session()
        client.turn_scripts.append("hang")
        turn = asyncio.create_task(_collect(session.send_turn("team orchestration")))
        await _wait_until(lambda: client.queries == ["team orchestration"])
        client.emit(
            SystemMessage(
                subtype="task_started",
                data={
                    "task_id": "teammate-1",
                    "task_type": "teammate",
                    "session_id": client.session_id,
                },
            )
        )
        client.emit_result(result="interactive run complete")
        events = await asyncio.wait_for(turn, timeout=2.0)

        self.assertIsInstance(events[-1], CompletionEvent)
        can_use_tool = client.options.can_use_tool
        self.assertIsNotNone(can_use_tool)
        assert can_use_tool is not None
        late = await can_use_tool(
            "Bash",
            {"command": "late-command"},
            ToolPermissionContext(tool_use_id="after-teammate-result"),
        )
        self.assertIsInstance(late, PermissionResultDeny)

    async def test_without_handler_frames_are_dropped_and_turns_unaffected(self) -> None:
        session, client = await self._start_session()

        client.emit_assistant("orphaned background text")
        client.emit_result(result=None)
        await self._drain(client)

        events = await _collect(session.send_turn("hello"))
        texts = [event.text for event in events if isinstance(event, TextDeltaEvent)]
        self.assertEqual(texts, ["turn answer"])

    async def test_handler_failure_never_breaks_the_reader_or_later_turns(self) -> None:
        session, client = await self._start_session()

        async def broken(text: str, session_id: str | None) -> None:
            raise RuntimeError("delivery route exploded")

        session.set_unsolicited_handler(broken)
        client.emit_assistant("background report")
        client.emit_result(result=None)
        await self._drain(client)

        events = await _collect(session.send_turn("hello"))
        texts = [event.text for event in events if isinstance(event, TextDeltaEvent)]
        self.assertEqual(texts, ["turn answer"])

        # Registration is replaceable: the next autonomous turn delivers.
        session.set_unsolicited_handler(self._handler)
        client.emit_assistant("second report")
        client.emit_result(result=None)
        await asyncio.wait_for(self.delivered_event.wait(), timeout=2.0)
        self.assertEqual(self.delivered, [("second report", client.session_id)])

    # -- ownership across interleaved turns --------------------------------

    async def test_inflight_autonomous_turn_keeps_frames_when_user_turn_arrives(
        self,
    ) -> None:
        session, client = await self._start_session()
        session.set_unsolicited_handler(self._handler)

        # The autonomous turn establishes ownership before any user turn.
        client.emit_assistant("autonomous progress")
        await _wait_until(lambda: session._unsolicited_inflight)

        # A user turn is submitted mid-autonomous-turn; its own frames are
        # controlled by hand ("hang" script).
        client.turn_scripts.append("hang")
        turn_task = asyncio.create_task(_collect(session.send_turn("user turn")))
        await _wait_until(lambda: client.queries == ["user turn"])

        # The autonomous turn's terminal result arrives AFTER the user turn
        # was submitted — the user turn must not steal it.
        client.emit_result(result=None)
        await asyncio.wait_for(self.delivered_event.wait(), timeout=2.0)
        self.assertEqual(self.delivered, [("autonomous progress", client.session_id)])

        # Only now do the user turn's frames arrive, and they belong to it.
        client.emit_assistant("user answer")
        client.emit_result(result="user answer")
        events = await asyncio.wait_for(turn_task, timeout=2.0)
        texts = [event.text for event in events if isinstance(event, TextDeltaEvent)]
        self.assertEqual(texts, ["user answer"])
        self.assertIsInstance(events[-1], CompletionEvent)
        self.assertEqual(len(self.delivered), 1)

    async def test_abandoned_turn_frames_are_swallowed_not_redelivered(self) -> None:
        session, client = await self._start_session()
        session.set_unsolicited_handler(self._handler)

        client.turn_scripts.append("hang")
        iterator = session.send_turn("will be abandoned").__aiter__()
        first_event = asyncio.create_task(iterator.__anext__())
        await _wait_until(lambda: client.queries == ["will be abandoned"])
        first_event.cancel()
        await asyncio.gather(first_event, return_exceptions=True)

        # The abandoned turn's late frames must be discarded through its
        # terminal result (the adapter counterpart of stall_swallow_result)…
        client.emit_assistant("late answer")
        client.emit_result(result="late answer")
        await self._drain(client)
        self.assertEqual(self.delivered, [])

        # …while a genuinely new autonomous turn afterwards still delivers.
        client.emit_assistant("fresh background report")
        client.emit_result(result=None)
        await asyncio.wait_for(self.delivered_event.wait(), timeout=2.0)
        self.assertEqual(
            self.delivered, [("fresh background report", client.session_id)]
        )

    async def test_abort_stalled_waiter_closes_owner_and_rotates_shared_lock(
        self,
    ) -> None:
        """#625: a waiter on another resumed session must not inherit the
        first session's permanently held turn lock after recovery."""

        owner, owner_client = await self._start_session()
        owner_client.turn_scripts.append("hang")
        owner_task = asyncio.create_task(_collect(owner.send_turn("owner")))
        await _wait_until(lambda: owner_client.queries == ["owner"])

        waiter = await self.runtime.start_or_resume(
            SessionRequest(
                working_directory="/workspace", session_id=owner.session_id
            )
        )
        waiter_client = self.clients[-1]
        waiter_task = asyncio.create_task(_collect(waiter.send_turn("waiter")))
        await asyncio.sleep(0.01)
        self.assertEqual(waiter_client.queries, [])

        await waiter.interrupt()
        waiter_task.cancel()
        await asyncio.gather(waiter_task, return_exceptions=True)
        await waiter.abort_stalled_turn()
        owner_events = await asyncio.wait_for(owner_task, timeout=2.0)

        self.assertEqual(owner_client.interrupts, 1)
        self.assertIsInstance(owner_events[-1], ErrorEvent)
        self.assertEqual(waiter_client.queries, [])

        recovered = await self.runtime.start_or_resume(
            SessionRequest(
                working_directory="/workspace", session_id=owner.session_id
            )
        )
        recovered_client = self.clients[-1]
        recovered_events = await asyncio.wait_for(
            _collect(recovered.send_turn("recovered")), timeout=2.0
        )
        self.assertEqual(recovered_client.queries, ["recovered"])
        self.assertIsInstance(recovered_events[-1], CompletionEvent)

    async def test_abort_timeout_keeps_disconnects_alive_and_rotates_lock(
        self,
    ) -> None:
        """#691: the caller's short abort timeout must not cancel either
        SDK disconnect or leave the shared lock poisoned."""

        owner, owner_client = await self._start_session()
        owner_client.turn_scripts.append("hang")
        owner_task = asyncio.create_task(_collect(owner.send_turn("owner")))
        await _wait_until(lambda: owner_client.queries == ["owner"])

        waiter = await self.runtime.start_or_resume(
            SessionRequest(
                working_directory="/workspace", session_id=owner.session_id
            )
        )
        waiter_client = self.clients[-1]
        waiter_task = asyncio.create_task(_collect(waiter.send_turn("waiter")))
        await asyncio.sleep(0.01)
        self.assertEqual(waiter_client.queries, [])

        owner_client.disconnect_gate = asyncio.Event()
        waiter_client.disconnect_gate = asyncio.Event()
        owner_lock = owner._turn_lock

        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(waiter.abort_stalled_turn(), timeout=0.01)

        await asyncio.wait_for(owner_client.disconnect_started.wait(), timeout=2.0)
        await asyncio.wait_for(waiter_client.disconnect_started.wait(), timeout=2.0)
        self.assertEqual(owner_client.disconnect_cancellations, 0)
        self.assertEqual(waiter_client.disconnect_cancellations, 0)
        self.assertIsNot(self.runtime._session_locks[owner.session_id], owner_lock)
        self.assertNotIn(owner.session_id, self.runtime._turn_owners)

        owner_client.disconnect_gate.set()
        waiter_client.disconnect_gate.set()
        await asyncio.gather(owner.close(), waiter.close())
        owner_events = await asyncio.wait_for(owner_task, timeout=2.0)
        waiter_outcome = await asyncio.wait_for(
            asyncio.gather(waiter_task, return_exceptions=True), timeout=2.0
        )
        self.assertIsInstance(owner_events[-1], ErrorEvent)
        self.assertIsInstance(waiter_outcome[0], RuntimeError)
        self.assertEqual(waiter_client.queries, [])
        self.assertEqual(owner_client.disconnect_calls, 1)
        self.assertEqual(waiter_client.disconnect_calls, 1)
        self.assertTrue(owner_client.disconnected.is_set())
        self.assertTrue(waiter_client.disconnected.is_set())

        recovered = await self.runtime.start_or_resume(
            SessionRequest(
                working_directory="/workspace", session_id=owner.session_id
            )
        )
        recovered_client = self.clients[-1]
        recovered_events = await asyncio.wait_for(
            _collect(recovered.send_turn("recovered")), timeout=2.0
        )
        self.assertEqual(recovered_client.queries, ["recovered"])
        self.assertIsInstance(recovered_events[-1], CompletionEvent)

    # -- raw SDK frame observation seam (#584 C-1 follow-up) ---------------

    async def test_frame_observer_sees_turn_unsolicited_and_rate_limit_frames(
        self,
    ) -> None:
        session, client = await self._start_session()
        session.set_unsolicited_handler(self._handler)
        frames: list[Message] = []
        session.set_sdk_frame_observer(frames.append)

        # Turn flow: the scripted assistant+result frames must be observed
        # while the turn's normalized event stream stays intact.
        events = await _collect(session.send_turn("hello"))
        texts = [event.text for event in events if isinstance(event, TextDeltaEvent)]
        self.assertEqual(texts, ["turn answer"])

        # Between-turns flow: rate-limit and unsolicited frames observed too.
        client.emit_rate_limit()
        client.emit_assistant("background report")
        client.emit_result(result=None)
        await asyncio.wait_for(self.delivered_event.wait(), timeout=2.0)
        await self._drain(client)

        self.assertEqual(
            [type(frame).__name__ for frame in frames],
            [
                "SystemMessage",  # first-query session initialization
                "AssistantMessage",  # turn flow
                "ResultMessage",  # turn terminal
                "RateLimitEvent",  # account-level, no owning turn
                "AssistantMessage",  # unsolicited flow
                "ResultMessage",  # unsolicited terminal
            ],
        )
        # Observation-only: unsolicited delivery still happened exactly once.
        self.assertEqual(self.delivered, [("background report", client.session_id)])

    async def test_broken_frame_observer_never_affects_turns_or_delivery(
        self,
    ) -> None:
        session, client = await self._start_session()
        session.set_unsolicited_handler(self._handler)

        def broken_observer(_message: Message) -> None:
            raise RuntimeError("observer exploded")

        session.set_sdk_frame_observer(broken_observer)

        events = await _collect(session.send_turn("hello"))
        texts = [event.text for event in events if isinstance(event, TextDeltaEvent)]
        self.assertEqual(texts, ["turn answer"])
        self.assertIsInstance(events[-1], CompletionEvent)

        client.emit_rate_limit()
        client.emit_assistant("background report")
        client.emit_result(result=None)
        await asyncio.wait_for(self.delivered_event.wait(), timeout=2.0)
        self.assertEqual(self.delivered, [("background report", client.session_id)])


if __name__ == "__main__":
    unittest.main()
