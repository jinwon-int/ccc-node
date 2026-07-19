"""Bind the AgentRuntime conformance suite to real adapters (#387).

Four bindings run here:

* ``ReferenceRuntimeConformanceTests`` — the normative in-memory runtime,
  proving the suite itself is satisfiable exactly as specified.
* ``CodexRuntimeConformanceTests`` — the real ``CodexRuntime`` adapter over a
  scripted fake app-server (no live provider, no subprocess).
* ``ClaudeRuntimeConformanceTests`` — the real ``ClaudeRuntime`` adapter over
  a scripted fake Claude SDK client (no live provider, no subprocess).
* ``NonConformantRuntimeRejectionTests`` — negative proof: a runtime that
  violates any single contract clause fails the corresponding suite test.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Mapping, Sequence
import json
from pathlib import Path
import sys
import tempfile
from typing import TYPE_CHECKING, Any
import unittest
import uuid


def _purge_injected_sdk_stubs() -> None:
    """Drop spec-less ``claude_agent_sdk`` stubs left by earlier test modules.

    ``test_project_chat_mixins_contract`` and ``test_project_chat_serialization``
    replace ``sys.modules["claude_agent_sdk"]`` with bare stub modules at import
    time and never restore the real package. This module drives the real
    ``ClaudeRuntime`` over real SDK frame types, so remove stub entries (plain
    ``ModuleType`` objects without a ``__spec__``) and let the import below load
    the installed package. The stubbing modules keep working: they re-install
    their stubs themselves and never re-import the package afterwards.
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
    ContentBlock,
    Message,
    PermissionResultAllow,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolPermissionContext,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

if TYPE_CHECKING:
    from core.agent_runtime import AgentRuntime, AgentSession, SessionBrowser
    from core.claude_runtime import ClaudeRuntime
    from core.codex_app_server import (
        CodexNotification,
        CodexServerRequest,
        CodexThread,
        CodexThreadListPage,
        CodexThreadSummary,
    )
    from core.codex_runtime import CodexRuntime
    from core.provider_capabilities import (
        PROVIDER_CAPABILITY_MATRIX,
        CapabilityState,
    )
    from tests import runtime_conformance as conformance
else:
    from telegram_bot.core.agent_runtime import (
        AgentRuntime,
        AgentSession,
        SessionBrowser,
    )
    from telegram_bot.core.claude_runtime import ClaudeRuntime
    from telegram_bot.core.codex_app_server import (
        CodexNotification,
        CodexServerRequest,
        CodexThread,
        CodexThreadListPage,
        CodexThreadSummary,
    )
    from telegram_bot.core.codex_runtime import CodexRuntime
    from telegram_bot.core.provider_capabilities import (
        PROVIDER_CAPABILITY_MATRIX,
        CapabilityState,
    )
    import runtime_conformance as conformance


class ReferenceRuntimeConformanceTests(conformance.AgentRuntimeConformanceSuite):
    """The normative reference runtime must pass its own contract."""

    def make_harness(self) -> conformance.ConformanceHarness:
        return conformance.ReferenceConformanceHarness()


class ScriptedCodexAppServer:
    """Deterministic fake Codex app-server driven by canonical turn scripts.

    Implements the subset of the ``AppServerClient`` protocol that
    ``CodexRuntime`` uses.  ``turn_start`` consumes the next queued script for
    the thread and emits the matching notifications/server requests, so the
    shared conformance scenarios run against the real adapter with no live
    provider.
    """

    def __init__(self, server_request_handler: Any) -> None:
        self._handler = server_request_handler
        self.turn_starts: list[tuple[str, str]] = []
        self.approval_decisions: list[str] = []
        self.gate = asyncio.Event()
        self.thread_pages: list[CodexThreadListPage] = []
        self.thread_reads: dict[str, CodexThread] = {}
        self._scripts: dict[str, deque[str]] = {}
        self._notifications: asyncio.Queue[CodexNotification] = asyncio.Queue()
        self._tasks: list[asyncio.Task[None]] = []
        self._thread_counter = 0
        self._turn_counter = 0

    # -- scripting ---------------------------------------------------------

    def queue_script(self, thread_id: str, kind: str) -> None:
        self._scripts.setdefault(thread_id, deque()).append(kind)

    def _emit(self, method: str, params: Mapping[str, Any]) -> None:
        self._notifications.put_nowait(CodexNotification(method, params))

    def _emit_agent_text(self, thread_id: str, turn_id: str, text: str) -> None:
        self._emit(
            "item/agentMessage/delta",
            {"threadId": thread_id, "turnId": turn_id, "delta": text},
        )
        self._emit(
            "item/completed",
            {
                "threadId": thread_id,
                "turnId": turn_id,
                "item": {"id": f"msg-{turn_id}", "type": "agentMessage", "text": text},
            },
        )

    def _emit_turn_completed(self, thread_id: str, turn_id: str, status: str) -> None:
        turn: dict[str, Any] = {"id": turn_id, "status": status}
        if status == "failed":
            turn["error"] = "scripted provider failure"
        self._emit("turn/completed", {"threadId": thread_id, "turn": turn})

    def _spawn(self, coro: Any) -> None:
        self._tasks.append(asyncio.create_task(coro))

    def _script_simple(self, thread_id: str, turn_id: str) -> None:
        self._emit(
            "item/reasoning/textDelta",
            {"threadId": thread_id, "turnId": turn_id, "delta": "planning the reply"},
        )
        self._emit(
            "item/agentMessage/delta",
            {"threadId": thread_id, "turnId": turn_id, "delta": "Hello, "},
        )
        self._emit(
            "item/agentMessage/delta",
            {"threadId": thread_id, "turnId": turn_id, "delta": "world"},
        )
        self._emit(
            "item/completed",
            {
                "threadId": thread_id,
                "turnId": turn_id,
                "item": {
                    "id": f"msg-{turn_id}",
                    "type": "agentMessage",
                    "text": conformance.SIMPLE_TURN_TEXT,
                },
            },
        )
        self._emit_turn_completed(thread_id, turn_id, "completed")

    def _script_two_messages(self, thread_id: str, turn_id: str) -> None:
        for text in conformance.TWO_MESSAGE_TEXTS:
            self._emit_agent_text(thread_id, turn_id, text)
        self._emit_turn_completed(thread_id, turn_id, "completed")

    def _script_tool(self, thread_id: str, turn_id: str) -> None:
        tool_item = {"id": f"call-{turn_id}", "type": "commandExecution", "command": "pwd"}
        self._emit(
            "item/started",
            {"threadId": thread_id, "turnId": turn_id, "item": dict(tool_item)},
        )
        self._emit(
            "item/completed",
            {
                "threadId": thread_id,
                "turnId": turn_id,
                "item": {**tool_item, "status": "completed", "exitCode": 0},
            },
        )
        self._emit_agent_text(thread_id, turn_id, conformance.TOOL_TURN_TEXT)
        self._emit_turn_completed(thread_id, turn_id, "completed")

    def _script_failure(self, thread_id: str, turn_id: str) -> None:
        self._emit_turn_completed(thread_id, turn_id, "failed")

    def _script_gated(self, thread_id: str, turn_id: str) -> None:
        self._emit(
            "item/agentMessage/delta",
            {"threadId": thread_id, "turnId": turn_id, "delta": conformance.GATED_FIRST_TEXT},
        )

        async def finish_after_gate() -> None:
            await self.gate.wait()
            self._emit(
                "item/completed",
                {
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "item": {
                        "id": f"msg-{turn_id}",
                        "type": "agentMessage",
                        "text": conformance.GATED_FIRST_TEXT,
                    },
                },
            )
            self._emit_turn_completed(thread_id, turn_id, "completed")

        self._spawn(finish_after_gate())

    def _script_approval(self, thread_id: str, turn_id: str) -> None:
        async def request_approval() -> None:
            request = CodexServerRequest(
                id=f"req-{turn_id}",
                method="item/commandExecution/requestApproval",
                params={
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "command": "rm -rf sandbox",
                },
            )
            response = await self._handler(request)
            result = response.get("result")
            decision = result.get("decision") if isinstance(result, Mapping) else None
            self.approval_decisions.append("allow" if decision == "accept" else "deny")
            self._emit_agent_text(thread_id, turn_id, conformance.APPROVAL_TURN_TEXT)
            self._emit_turn_completed(thread_id, turn_id, "completed")

        self._spawn(request_approval())

    # -- AppServerClient protocol -------------------------------------------

    async def start(self) -> Any:
        return {}

    async def thread_start(self, *, cwd: str, model: str | None = None) -> Any:
        self._thread_counter += 1
        return {"thread": {"id": f"thread-{self._thread_counter}"}}

    async def thread_resume(
        self,
        thread_id: str,
        *,
        cwd: str | None = None,
        model: str | None = None,
    ) -> Any:
        return {"thread": {"id": thread_id}}

    async def thread_rollback(self, thread_id: str, *, num_turns: int) -> Any:
        raise AssertionError("conformance scripts never roll back threads")

    async def turn_start(
        self,
        thread_id: str,
        input_items: Sequence[Mapping[str, Any]],
        *,
        model: str | None = None,
        effort: str | None = None,
        approval_policy: str | None = None,
        approvals_reviewer: str | None = None,
        sandbox_policy: Mapping[str, Any] | None = None,
    ) -> Any:
        self._turn_counter += 1
        turn_id = f"turn-{self._turn_counter}"
        message = ""
        if input_items:
            text = input_items[0].get("text")
            if isinstance(text, str):
                message = text
        self.turn_starts.append((thread_id, message))
        queued = self._scripts.get(thread_id)
        script = queued.popleft() if queued else "simple"
        if script == "two_messages":
            self._script_two_messages(thread_id, turn_id)
        elif script == "tool":
            self._script_tool(thread_id, turn_id)
        elif script == "approval":
            self._script_approval(thread_id, turn_id)
        elif script == "failure":
            self._script_failure(thread_id, turn_id)
        elif script == "gated":
            self._script_gated(thread_id, turn_id)
        elif script == "hang":
            pass  # nothing arrives until turn_interrupt
        else:
            self._script_simple(thread_id, turn_id)
        return {"turn": {"id": turn_id}}

    async def turn_interrupt(self, thread_id: str, turn_id: str) -> Any:
        self._emit_turn_completed(thread_id, turn_id, "interrupted")
        return {}

    async def list_models(self, *, include_hidden: bool = False) -> Any:
        return {
            "data": [
                {
                    "id": "codex-conformance-model",
                    "displayName": "Codex conformance model",
                    "isDefault": True,
                    "defaultReasoningEffort": "medium",
                    "supportedReasoningEfforts": [{"reasoningEffort": "medium"}],
                }
            ]
        }

    async def account_rate_limits(self) -> Any:
        return {"rateLimits": {}}

    async def account_usage(self) -> Any:
        return {"summary": {}}

    async def thread_list(
        self, *, limit: int = 20, cursor: str | None = None
    ) -> CodexThreadListPage:
        if self.thread_pages:
            return self.thread_pages.pop(0)
        return CodexThreadListPage(data=(), next_cursor=None)

    async def thread_read(
        self, thread_id: str, *, include_turns: bool = True
    ) -> CodexThread | None:
        return self.thread_reads.get(thread_id)

    async def next_notification(self) -> CodexNotification:
        return await self._notifications.get()

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()


class CodexConformanceHarness(conformance.ConformanceHarness):
    """Drive the real CodexRuntime adapter over the scripted app-server."""

    provider = "codex"

    def __init__(self) -> None:
        super().__init__()
        self._client: ScriptedCodexAppServer | None = None
        self._runtime: CodexRuntime | None = None

    async def start(self) -> None:
        def factory(handler: Any) -> ScriptedCodexAppServer:
            self._client = ScriptedCodexAppServer(handler)
            return self._client

        self._runtime = CodexRuntime(client_factory=factory)

    async def close(self) -> None:
        if self._runtime is not None:
            await self._runtime.close()
        self._runtime = None
        self._client = None

    @property
    def runtime(self) -> AgentRuntime:
        assert self._runtime is not None, "harness not started"
        return self._runtime

    @property
    def client(self) -> ScriptedCodexAppServer:
        assert self._client is not None, "the runtime has not started its client"
        return self._client

    def capability_state(self, axis_key: str) -> CapabilityState:
        return PROVIDER_CAPABILITY_MATRIX["codex"][axis_key].state

    def arrange_turn(
        self, session: AgentSession, kind: conformance.TurnScriptKind
    ) -> None:
        self.client.queue_script(session.session_id, kind)

    def release_gated_turn(self) -> None:
        self.client.gate.set()

    def provider_turn_starts(self) -> Sequence[tuple[str, str]]:
        return tuple(self.client.turn_starts)

    def provider_approval_decisions(self) -> Sequence[str]:
        return tuple(self.client.approval_decisions)

    def session_browser(self) -> SessionBrowser:
        assert self._runtime is not None, "harness not started"
        return self._runtime

    def arrange_stored_sessions(self) -> str:
        stored_id = "thread-stored-1"
        self.client.thread_pages = [
            CodexThreadListPage(
                data=(
                    CodexThreadSummary(
                        stored_id, "Stored session", "hello", 42.0, "/workspace", "codex"
                    ),
                ),
                next_cursor=None,
            )
        ]
        self.client.thread_reads[stored_id] = CodexThread(
            id=stored_id,
            turns=(
                {
                    "id": "turn-stored-1",
                    "createdAt": "2026-07-15T00:00:00Z",
                    "items": [
                        {"type": "userMessage", "content": "hello"},
                        {"type": "agentMessage", "text": "hi"},
                    ],
                },
            ),
        )
        return stored_id

    async def wait_until_interruptible(self, session: AgentSession) -> None:
        # CodexSession.interrupt() is a no-op until the adapter has recorded
        # the provider turn id, which happens right after turn/start returns.
        # Waiting only for the provider call would race that window, so poll
        # the adapter's active-turn registry directly.
        assert self._runtime is not None, "harness not started"
        runtime = self._runtime

        async def poll() -> None:
            while True:
                active = runtime._active_turns.get(session.session_id)
                if active is not None and active.turn_id is not None:
                    return
                await asyncio.sleep(0.005)

        await asyncio.wait_for(poll(), timeout=self.liveness_timeout)


class CodexRuntimeConformanceTests(conformance.AgentRuntimeConformanceSuite):
    """The real Codex adapter must satisfy the shared behavior contract."""

    def make_harness(self) -> conformance.ConformanceHarness:
        return CodexConformanceHarness()


class _ClaudeScriptState:
    """Shared recorder for every scripted Claude SDK client in one harness."""

    def __init__(self) -> None:
        self.turn_starts: list[tuple[str, str]] = []
        self.approval_decisions: list[str] = []
        self.gate = asyncio.Event()
        self.scripts: dict[str, deque[str]] = {}
        self.session_counter = 0
        self.turn_counter = 0

    def queue_script(self, session_id: str, kind: str) -> None:
        self.scripts.setdefault(session_id, deque()).append(kind)


class ScriptedClaudeSdkClient:
    """Deterministic fake Claude SDK client driven by canonical turn scripts.

    Implements the subset of the ``ClaudeSDKClient`` surface that
    ``ClaudeRuntime`` uses.  ``connect`` announces the SDK session id via a
    ``system`` frame (as the CLI does at startup) and ``query`` consumes the
    next queued script for the session, emitting the matching SDK frames, so
    the shared conformance scenarios run against the real adapter with no
    live provider.
    """

    def __init__(self, options: ClaudeAgentOptions, state: _ClaudeScriptState) -> None:
        self._options = options
        self._state = state
        if options.resume:
            self.session_id = options.resume
        else:
            state.session_counter += 1
            self.session_id = f"claude-session-{state.session_counter}"
        self._messages: asyncio.Queue[Message | None] = asyncio.Queue()
        self._tasks: list[asyncio.Task[None]] = []

    # -- frame builders ----------------------------------------------------

    def _emit(self, message: Message) -> None:
        self._messages.put_nowait(message)

    def _emit_stream_delta(self, text: str) -> None:
        self._emit(
            StreamEvent(
                uuid=str(uuid.uuid4()),
                session_id=self.session_id,
                event={
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": text},
                },
            )
        )

    def _emit_assistant(self, blocks: Sequence[ContentBlock]) -> None:
        self._emit(
            AssistantMessage(
                content=list(blocks),
                model="claude-conformance-model",
                session_id=self.session_id,
            )
        )

    def _emit_result(
        self,
        *,
        subtype: str = "success",
        is_error: bool = False,
        result: str | None = None,
    ) -> None:
        self._emit(
            ResultMessage(
                subtype=subtype,
                duration_ms=5,
                duration_api_ms=3,
                is_error=is_error,
                num_turns=1,
                session_id=self.session_id,
                result=result,
            )
        )

    def _spawn(self, coro: Any) -> None:
        self._tasks.append(asyncio.create_task(coro))

    # -- scripts -----------------------------------------------------------

    def _script_simple(self) -> None:
        # Token deltas stream first; the completed assistant message repeats
        # the same text, proving the adapter's partial-stream dedupe.
        self._emit_stream_delta("Hello, ")
        self._emit_stream_delta("world")
        self._emit_assistant(
            [
                ThinkingBlock(thinking="planning the reply", signature="sig"),
                TextBlock(text=conformance.SIMPLE_TURN_TEXT),
            ]
        )
        self._emit_result(result=conformance.SIMPLE_TURN_TEXT)

    def _script_two_messages(self) -> None:
        for text in conformance.TWO_MESSAGE_TEXTS:
            self._emit_assistant([TextBlock(text=text)])
        self._emit_result(result=conformance.TWO_MESSAGE_TEXTS[-1])

    def _script_tool(self, turn_id: str) -> None:
        tool_use_id = f"toolu-{turn_id}"
        self._emit_assistant(
            [ToolUseBlock(id=tool_use_id, name="Bash", input={"command": "pwd"})]
        )
        tool_results: list[ContentBlock] = [
            ToolResultBlock(
                tool_use_id=tool_use_id,
                content=[{"type": "text", "text": "/workspace"}],
                is_error=False,
            )
        ]
        self._emit(UserMessage(content=tool_results))
        self._emit_assistant([TextBlock(text=conformance.TOOL_TURN_TEXT)])
        self._emit_result(result=conformance.TOOL_TURN_TEXT)

    def _script_approval(self, turn_id: str) -> None:
        async def request_approval() -> None:
            can_use_tool = self._options.can_use_tool
            assert can_use_tool is not None, "the adapter must install can_use_tool"
            context = ToolPermissionContext(
                tool_use_id=f"toolu-approval-{turn_id}",
                title="Claude wants to run rm -rf sandbox",
            )
            result = await can_use_tool("Bash", {"command": "rm -rf sandbox"}, context)
            self._state.approval_decisions.append(
                "allow" if isinstance(result, PermissionResultAllow) else "deny"
            )
            self._emit_assistant([TextBlock(text=conformance.APPROVAL_TURN_TEXT)])
            self._emit_result(result=conformance.APPROVAL_TURN_TEXT)

        self._spawn(request_approval())

    def _script_failure(self) -> None:
        self._emit_assistant([TextBlock(text="partial answer before the failure")])
        self._emit_result(
            subtype="error_during_execution",
            is_error=True,
            result="Scripted provider failure",
        )

    def _script_gated(self) -> None:
        self._emit_stream_delta(conformance.GATED_FIRST_TEXT)

        async def finish_after_gate() -> None:
            await self._state.gate.wait()
            self._emit_assistant([TextBlock(text=conformance.GATED_FIRST_TEXT)])
            self._emit_result(result=conformance.GATED_FIRST_TEXT)

        self._spawn(finish_after_gate())

    # -- SdkClient protocol ------------------------------------------------

    async def connect(self) -> None:
        self._emit(
            SystemMessage(
                subtype="init",
                data={"session_id": self.session_id, "cwd": "/workspace"},
            )
        )

    async def receive_messages(self) -> AsyncIterator[Message]:
        while True:
            message = await self._messages.get()
            if message is None:
                return
            yield message

    async def query(self, prompt: str) -> None:
        self._state.turn_starts.append((self.session_id, prompt))
        self._state.turn_counter += 1
        turn_id = f"turn-{self._state.turn_counter}"
        queued = self._state.scripts.get(self.session_id)
        script = queued.popleft() if queued else "simple"
        if script == "two_messages":
            self._script_two_messages()
        elif script == "tool":
            self._script_tool(turn_id)
        elif script == "approval":
            self._script_approval(turn_id)
        elif script == "failure":
            self._script_failure()
        elif script == "gated":
            self._script_gated()
        elif script == "hang":
            pass  # nothing arrives until interrupt()
        else:
            self._script_simple()

    async def interrupt(self) -> None:
        self._emit_result(
            subtype="error_during_execution",
            is_error=True,
            result="Turn was interrupted",
        )

    async def disconnect(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self._messages.put_nowait(None)


class ClaudeConformanceHarness(conformance.ConformanceHarness):
    """Drive the real ClaudeRuntime adapter over scripted fake SDK clients."""

    provider = "claude"

    def __init__(self) -> None:
        super().__init__()
        self._state: _ClaudeScriptState | None = None
        self._runtime: ClaudeRuntime | None = None
        self._transcripts_dir: tempfile.TemporaryDirectory[str] | None = None

    async def start(self) -> None:
        state = _ClaudeScriptState()
        self._state = state
        self._transcripts_dir = tempfile.TemporaryDirectory()

        def factory(options: ClaudeAgentOptions) -> ScriptedClaudeSdkClient:
            return ScriptedClaudeSdkClient(options, state)

        self._runtime = ClaudeRuntime(
            sdk_client_factory=factory,
            transcripts_dir=self._transcripts_dir.name,
        )

    async def close(self) -> None:
        if self._runtime is not None:
            await self._runtime.close()
        self._runtime = None
        self._state = None
        if self._transcripts_dir is not None:
            self._transcripts_dir.cleanup()
            self._transcripts_dir = None

    @property
    def runtime(self) -> AgentRuntime:
        assert self._runtime is not None, "harness not started"
        return self._runtime

    @property
    def state(self) -> _ClaudeScriptState:
        assert self._state is not None, "harness not started"
        return self._state

    def capability_state(self, axis_key: str) -> CapabilityState:
        # The committed matrix still describes the live project_chat path;
        # the claude column flips only at the #584 cutover. The adapter
        # itself must satisfy every covered axis now, so the harness runs
        # the full suite instead of skipping on the pre-cutover matrix.
        return CapabilityState.SUPPORTED

    def arrange_turn(
        self, session: AgentSession, kind: conformance.TurnScriptKind
    ) -> None:
        self.state.queue_script(session.session_id, kind)

    def release_gated_turn(self) -> None:
        self.state.gate.set()

    def provider_turn_starts(self) -> Sequence[tuple[str, str]]:
        return tuple(self.state.turn_starts)

    def provider_approval_decisions(self) -> Sequence[str]:
        return tuple(self.state.approval_decisions)

    def session_browser(self) -> SessionBrowser:
        assert self._runtime is not None, "harness not started"
        return self._runtime

    def arrange_stored_sessions(self) -> str:
        assert self._transcripts_dir is not None, "harness not started"
        stored_id = "claude-stored-1"
        lines = [
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "hello"}],
                },
                "timestamp": "2026-07-15T00:00:00Z",
            },
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "hi"}],
                },
                "timestamp": "2026-07-15T00:00:01Z",
            },
        ]
        path = Path(self._transcripts_dir.name) / f"{stored_id}.jsonl"
        path.write_text(
            "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8"
        )
        return stored_id


class ClaudeRuntimeConformanceTests(conformance.AgentRuntimeConformanceSuite):
    """The real Claude adapter must satisfy the shared behavior contract."""

    def make_harness(self) -> conformance.ConformanceHarness:
        return ClaudeConformanceHarness()


class NonConformantRuntimeRejectionTests(unittest.TestCase):
    """A runtime that violates any single contract clause must fail the suite."""

    # (violation switch, suite test that must reject it)
    CASES: tuple[tuple[str, str], ...] = (
        (
            "empty_session_id",
            "test_new_session_exposes_stable_nonempty_session_id",
        ),
        (
            "completion_before_result",
            "test_simple_turn_streams_text_then_result_then_completion",
        ),
        (
            "events_after_terminal",
            "test_simple_turn_streams_text_then_result_then_completion",
        ),
        (
            "missing_terminal",
            "test_simple_turn_streams_text_then_result_then_completion",
        ),
        (
            "auto_allow_by_default",
            "test_omitted_approval_handler_is_fail_closed_deny",
        ),
        (
            "ignore_interrupt",
            "test_interrupt_terminates_inflight_turn_with_interrupted_code",
        ),
        (
            "interleave_turns",
            "test_concurrent_turns_on_one_session_serialize",
        ),
        (
            "tool_completed_without_started",
            "test_tool_lifecycle_events_pair_within_the_turn",
        ),
    )

    @staticmethod
    def _run_suite_method(violations: frozenset[str], method_name: str) -> unittest.TestResult:
        class ViolatingRuntimeConformanceTests(conformance.AgentRuntimeConformanceSuite):
            def make_harness(self) -> conformance.ConformanceHarness:
                return conformance.ReferenceConformanceHarness(
                    violations=violations,
                    liveness_timeout=0.5,
                )

        result = unittest.TestResult()
        unittest.TestSuite([ViolatingRuntimeConformanceTests(method_name)]).run(result)
        return result

    def test_violation_switches_are_all_known_to_the_reference_runtime(self) -> None:
        self.assertEqual(
            {violation for violation, _method in self.CASES},
            set(conformance.KNOWN_VIOLATIONS),
            "every documented violation mode needs a rejection case (and vice versa)",
        )

    def test_each_contract_violation_fails_its_suite_test(self) -> None:
        for violation, method_name in self.CASES:
            with self.subTest(violation=violation):
                result = self._run_suite_method(frozenset({violation}), method_name)
                self.assertEqual(result.testsRun, 1)
                self.assertFalse(
                    result.wasSuccessful(),
                    f"violation {violation!r} must fail {method_name}",
                )

    def test_the_same_suite_tests_pass_without_violations(self) -> None:
        for _violation, method_name in self.CASES:
            with self.subTest(method=method_name):
                result = self._run_suite_method(frozenset(), method_name)
                self.assertEqual(result.testsRun, 1)
                self.assertTrue(
                    result.wasSuccessful(),
                    f"{method_name} must pass on the compliant reference runtime: "
                    f"{result.failures or result.errors}",
                )


if __name__ == "__main__":
    unittest.main()
