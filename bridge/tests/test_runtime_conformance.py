"""Bind the AgentRuntime conformance suite to real adapters (#387).

Three bindings run here:

* ``ReferenceRuntimeConformanceTests`` — the normative in-memory runtime,
  proving the suite itself is satisfiable exactly as specified.
* ``CodexRuntimeConformanceTests`` — the real ``CodexRuntime`` adapter over a
  scripted fake app-server (no live provider, no subprocess).
* ``NonConformantRuntimeRejectionTests`` — negative proof: a runtime that
  violates any single contract clause fails the corresponding suite test.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any
import unittest

if TYPE_CHECKING:
    from core.agent_runtime import AgentRuntime, AgentSession, SessionBrowser
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
