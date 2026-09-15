"""Provider-neutral agent runtime contract tests."""

from __future__ import annotations

import ast
import asyncio
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
import importlib
import os
from pathlib import Path
import subprocess
import sys
import textwrap
from typing import TYPE_CHECKING, Any, cast
import unittest

if TYPE_CHECKING:
    from core.agent_runtime import (
        AgentEvent,
        AgentRuntime,
        AgentSession,
        AsyncCompletionCapability,
        ApprovalDecision,
        ApprovalHandler,
        ApprovalRequestEvent,
        CompletionEvent,
        DelegatedTaskLifecycleEvent,
        ErrorEvent,
        JsonValue,
        ModelInfo,
        ReasoningDeltaEvent,
        ResultEvent,
        SessionRequest,
        TextDeltaEvent,
        ToolCompletedEvent,
        ToolStartedEvent,
        deny_approval,
    )
    from core.claude_runtime import ClaudeRuntime, _ActiveTurn
else:
    from telegram_bot.core.agent_runtime import (
        AgentEvent,
        AgentRuntime,
        AgentSession,
        AsyncCompletionCapability,
        ApprovalDecision,
        ApprovalHandler,
        ApprovalRequestEvent,
        CompletionEvent,
        DelegatedTaskLifecycleEvent,
        ErrorEvent,
        JsonValue,
        ModelInfo,
        ReasoningDeltaEvent,
        ResultEvent,
        SessionRequest,
        TextDeltaEvent,
        ToolCompletedEvent,
        ToolStartedEvent,
        deny_approval,
    )
    from telegram_bot.core.claude_runtime import ClaudeRuntime, _ActiveTurn


from telegram_bot.core.agent_runtime import (
    SessionHistory,
    SessionHistoryMessage,
    SessionSummary,
)


class FakeSession:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.interrupted = False

    def send_turn(
        self,
        message: str,
        *,
        approval_handler: ApprovalHandler = deny_approval,
    ) -> AsyncIterator[AgentEvent]:
        request = ApprovalRequestEvent(
            request_id="approval-1",
            action="write_file",
            arguments={"path": "notes.txt"},
            description="Write notes.txt",
        )
        async def events() -> AsyncIterator[AgentEvent]:
            decision = await approval_handler(request)
            yield TextDeltaEvent(text=message)
            yield ReasoningDeltaEvent(text="checking approval")
            yield request
            if decision is ApprovalDecision.ALLOW:
                # Normative terminal ordering (see tests/runtime_conformance.py):
                # the ResultEvent precedes the terminal CompletionEvent, which
                # is always the final event of a turn stream.
                yield ResultEvent(result={"status": "written"})
                yield CompletionEvent(stop_reason="end_turn")
            else:
                yield ErrorEvent(code="approval_denied", message="Approval denied")

        return events()

    async def interrupt(self) -> None:
        self.interrupted = True


class FakeRuntime:
    async def start_or_resume(self, request: SessionRequest) -> AgentSession:
        return FakeSession(request.session_id or "new-session")

    async def list_models(self) -> Sequence[ModelInfo]:
        return (ModelInfo(id="fake-model", display_name="Fake model"),)


# Static type checkers must accept these structural implementations.
_fake_session_conforms: AgentSession = FakeSession("typed-session")
_fake_runtime_conforms: AgentRuntime = FakeRuntime()


class AgentRuntimeContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_fake_runtime_structurally_conforms_and_streams_all_event_kinds(self) -> None:
        runtime = FakeRuntime()
        session = await runtime.start_or_resume(
            SessionRequest(working_directory="/workspace", session_id="resume-me")
        )
        self.assertEqual(session.session_id, "resume-me")
        self.assertEqual((await runtime.list_models())[0].id, "fake-model")

        async def allow(_request: ApprovalRequestEvent) -> ApprovalDecision:
            return ApprovalDecision.ALLOW

        stream = session.send_turn("hello", approval_handler=allow)
        events = [event async for event in stream]

        self.assertEqual(
            [event.kind for event in events],
            ["text_delta", "reasoning_delta", "approval_request", "result", "completion"],
        )
        await session.interrupt()
        self.assertTrue(cast(FakeSession, session).interrupted)

    async def test_approval_is_explicit_and_defaults_to_deny(self) -> None:
        session = FakeSession("session")

        stream = session.send_turn("hello")
        events = [event async for event in stream]

        self.assertIsInstance(events[-1], ErrorEvent)
        self.assertEqual(cast(ErrorEvent, events[-1]).code, "approval_denied")
        request = cast(ApprovalRequestEvent, events[-2])
        self.assertEqual(request.arguments, {"path": "notes.txt"})

    async def test_normalized_events_enforce_non_empty_required_fields(self) -> None:
        invalid_factories: tuple[Callable[[], object], ...] = (
            lambda: TextDeltaEvent(text=""),
            lambda: ReasoningDeltaEvent(text=""),
            lambda: ApprovalRequestEvent(
                request_id="", action="write_file", arguments={}, description="Write a file"
            ),
            lambda: ApprovalRequestEvent(
                request_id="approval", action="", arguments={}, description="Write a file"
            ),
            lambda: ApprovalRequestEvent(
                request_id="approval", action="write_file", arguments={}, description=""
            ),
            lambda: CompletionEvent(stop_reason=""),
            lambda: DelegatedTaskLifecycleEvent(0, 0.0, "terminal"),
            lambda: DelegatedTaskLifecycleEvent(1, None, "started"),
            lambda: DelegatedTaskLifecycleEvent(-1, None, "terminal"),
            lambda: DelegatedTaskLifecycleEvent(1, float("inf"), "updated"),
            lambda: DelegatedTaskLifecycleEvent(1, float("nan"), "updated"),
            lambda: ErrorEvent(code="", message="failed"),
            lambda: ErrorEvent(code="failed", message=""),
            lambda: SessionRequest(working_directory=""),
            lambda: SessionRequest(working_directory="/workspace", session_id=""),
            lambda: SessionRequest(working_directory="/workspace", model=""),
            lambda: SessionRequest(working_directory="/workspace", effort=""),
            lambda: SessionRequest(working_directory="/workspace", approvals_reviewer=""),
            lambda: SessionRequest(working_directory="/workspace", sandbox_policy={}),
            lambda: ModelInfo(id="", display_name="Fake model"),
            lambda: ModelInfo(id="fake", display_name=""),
        )

        for factory in invalid_factories:
            with self.subTest(factory=factory), self.assertRaises(ValueError):
                factory()

    async def test_delegated_task_lifecycle_event_is_body_free(self) -> None:
        event = DelegatedTaskLifecycleEvent(4, 301.5, "updated")

        self.assertEqual(event.kind, "delegated_task_lifecycle")
        self.assertEqual(event.active_count, 4)
        self.assertEqual(event.oldest_age_seconds, 301.5)
        self.assertNotIn("task_id", repr(event))
        self.assertNotIn("session", repr(event))

    async def test_async_completion_capability_fails_closed(self) -> None:
        degraded = AsyncCompletionCapability(
            provider="codex",
            state="degraded",
            protocol_version=None,
            notification_method="turn/completed",
            recovery_method="thread/read",
            ownership_scope="exact_active_turn",
            supports_durable_delivery=False,
            reason_code="detached_ownership_unavailable",
        )

        self.assertEqual(degraded.state, "degraded")
        self.assertIsNone(degraded.protocol_version)
        invalid_factories: tuple[Callable[[], object], ...] = (
            lambda: AsyncCompletionCapability(
                provider="codex",
                state=cast(Any, "unknown"),
                protocol_version=None,
                notification_method=None,
                recovery_method=None,
                ownership_scope="exact_active_turn",
                supports_durable_delivery=False,
                reason_code="unknown_version",
            ),
            lambda: AsyncCompletionCapability(
                provider="",
                state="degraded",
                protocol_version=None,
                notification_method="turn/completed",
                recovery_method="thread/read",
                ownership_scope="exact_active_turn",
                supports_durable_delivery=False,
                reason_code="unavailable",
            ),
            lambda: AsyncCompletionCapability(
                provider="codex",
                state="degraded",
                protocol_version=None,
                notification_method="turn/completed",
                recovery_method="thread/read",
                ownership_scope="exact_active_turn",
                supports_durable_delivery=True,
                reason_code="unavailable",
            ),
            lambda: AsyncCompletionCapability(
                provider="codex",
                state="supported",
                protocol_version=None,
                notification_method="turn/completed",
                recovery_method="thread/read",
                ownership_scope="exact_active_turn",
                supports_durable_delivery=True,
                reason_code="supported",
            ),
        )
        for factory in invalid_factories:
            with self.subTest(factory=factory), self.assertRaises(ValueError):
                factory()

    async def test_event_payloads_are_recursively_immutable_snapshots(self) -> None:
        arguments: dict[str, JsonValue] = {"path": "notes.txt", "tags": ["a"]}
        request = ApprovalRequestEvent(
            request_id="approval-1",
            action="write_file",
            arguments=arguments,
            description="Write notes.txt",
        )
        result_source: dict[str, JsonValue] = {"items": [{"value": 1}]}
        result = ResultEvent(result=result_source)

        arguments["path"] = "changed.txt"
        cast(list[str], arguments["tags"]).append("b")
        cast(list[dict[str, int]], result_source["items"])[0]["value"] = 2

        self.assertEqual(request.arguments["path"], "notes.txt")
        self.assertEqual(request.arguments["tags"], ("a",))
        frozen_result = cast(Mapping[str, object], result.result)
        self.assertEqual(cast(tuple[Mapping[str, int], ...], frozen_result["items"])[0]["value"], 1)
        with self.assertRaises(TypeError):
            cast(dict[str, object], request.arguments)["path"] = "forbidden"

    async def test_session_request_sandbox_policy_is_recursively_immutable_snapshot(
        self,
    ) -> None:
        sandbox: dict[str, JsonValue] = {
            "type": "workspaceWrite",
            "networkAccess": False,
            "writableRoots": ["/workspace"],
        }
        request = SessionRequest(
            working_directory="/workspace",
            sandbox_policy=sandbox,
        )

        sandbox["networkAccess"] = True
        cast(list[str], sandbox["writableRoots"]).append("/tmp")

        assert request.sandbox_policy is not None
        self.assertFalse(request.sandbox_policy["networkAccess"])
        self.assertEqual(request.sandbox_policy["writableRoots"], ("/workspace",))
        with self.assertRaises(TypeError):
            cast(dict[str, JsonValue], request.sandbox_policy)["networkAccess"] = True

    async def test_session_request_memory_environment_is_validated_and_immutable(
        self,
    ) -> None:
        environment = {
            "CODEX_HOME": "/memory/private-opaque/codex",
            "CCC_MEMORY_SCOPE": "private-opaque",
        }
        request = SessionRequest(
            working_directory="/workspace",
            memory_environment=environment,
        )

        environment["CODEX_HOME"] = "/memory/leaking-global"

        assert request.memory_environment is not None
        self.assertEqual(
            request.memory_environment["CODEX_HOME"],
            "/memory/private-opaque/codex",
        )
        with self.assertRaises(TypeError):
            cast(dict[str, str], request.memory_environment)["CODEX_HOME"] = "/forbidden"
        for invalid in ({"": "value"}, {"BAD\x00NAME": "value"}, {"NAME": "bad\x00value"}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                SessionRequest(
                    working_directory="/workspace",
                    memory_environment=invalid,
                )

    async def test_tool_lifecycle_events_are_typed_immutable_snapshots(self) -> None:
        arguments: dict[str, JsonValue] = {"command": "pwd", "paths": ["."]}
        output: dict[str, JsonValue] = {"exitCode": 0, "lines": ["/workspace"]}

        started = ToolStartedEvent(tool_call_id="item-1", tool_name="command", arguments=arguments)
        completed = ToolCompletedEvent(
            tool_call_id="item-1", tool_name="command", result=output, success=True
        )
        cast(list[str], arguments["paths"]).append("changed")
        cast(list[str], output["lines"]).append("changed")

        self.assertEqual(started.kind, "tool_started")
        self.assertEqual(started.arguments["paths"], (".",))
        self.assertEqual(completed.kind, "tool_completed")
        self.assertEqual(cast(Mapping[str, object], completed.result)["lines"], ("/workspace",))


    async def test_session_browsing_values_are_immutable_and_validate_required_fields(self) -> None:
        summary = SessionSummary(
            id="thread-1",
            title="A title",
            preview="hello",
            updated_at=123.0,
            cwd="/workspace",
            model="codex-test",
        )
        message = SessionHistoryMessage(
            role="user", content="hello", timestamp="2026-01-01T00:00:00Z"
        )
        history = SessionHistory(session_id="thread-1", messages=[message])

        self.assertEqual(summary.id, "thread-1")
        self.assertEqual(history.messages, (message,))
        with self.assertRaises((AttributeError, TypeError)):
            cast(Any, history.messages).append(message)
        with self.assertRaises(ValueError):
            SessionSummary(id="")
        with self.assertRaises(ValueError):
            SessionHistoryMessage(role="tool", content="hidden")
        with self.assertRaises(ValueError):
            SessionHistory(session_id="", messages=())


    async def test_error_message_includes_diagnostic_fields_when_result_empty(self) -> None:
        """Per #901: is_error=True with empty result should show subtype/api_error_status/terminal_reason."""
        from claude_agent_sdk import ResultMessage

        # Build a minimal runtime to invoke _complete_turn.
        runtime = ClaudeRuntime()
        session = await runtime.start_or_resume(
            SessionRequest(working_directory="/tmp", model="sonnet")
        )
        active = _ActiveTurn(
            queue=asyncio.Queue(),
            approval_handler=deny_approval,
            generation=1,
        )

        def assert_error_message(
            *,
            subtype: str | None = None,
            api_error_status: int | None = None,
            terminal_reason: str | None = None,
            expected_snippet: str,
        ) -> None:
            msg = ResultMessage(
                subtype=subtype or "",
                result="",  # Empty result triggers diagnostic inclusion
                api_error_status=api_error_status,
                terminal_reason=terminal_reason,
                stop_reason="end_turn",
                duration_ms=0,
                duration_api_ms=0,
                is_error=True,
                num_turns=1,
                session_id="test-session",
            )
            session._complete_turn(active, msg)
            error = active.queue.get_nowait()
            self.assertIsInstance(error, ErrorEvent)
            text = cast(ErrorEvent, error).message
            self.assertIn(expected_snippet, text)

        # subtype only
        assert_error_message(
            subtype="rate_limit_error", expected_snippet="(subtype: rate_limit_error)"
        )

        # api_error_status only
        assert_error_message(api_error_status=429, expected_snippet="(HTTP status: 429)")

        # terminal_reason only
        assert_error_message(
            terminal_reason="max_tokens_reached", expected_snippet="(reason: max_tokens_reached)"
        )

        # All three fields
        assert_error_message(
            subtype="rate_limit_error",
            api_error_status=429,
            terminal_reason="quota_exceeded",
            expected_snippet="(subtype: rate_limit_error)",  # First field present
        )

        # Non-empty result uses the result text (legacy behavior)
        msg = ResultMessage(
            subtype="rate_limit_error",
            result="Custom error text",  # Non-empty
            api_error_status=429,
            terminal_reason="quota_exceeded",
            stop_reason="end_turn",
            duration_ms=0,
            duration_api_ms=0,
            is_error=True,
            num_turns=1,
            session_id="test-session",
        )
        session._complete_turn(active, msg)
        error = active.queue.get_nowait()
        self.assertIsInstance(error, ErrorEvent)
        self.assertEqual(cast(ErrorEvent, error).message, "Custom error text")

        await runtime.close()


_BRIDGE_DIR = Path(__file__).resolve().parents[1]
_CONTRACTS_DIR = _BRIDGE_DIR / "contracts"


def _declared_public_names(source_path: Path) -> frozenset[str]:
    """Public module-level names *defined* in ``source_path``.

    Read from the source rather than ``vars(module)`` so the imports the module
    makes for its own use (``Mapping``, ``Protocol``, ``math``, ...) are not
    mistaken for part of the contract, and so a newly added public name is
    picked up here the moment it lands.
    """

    names: set[str] = set()
    for node in ast.parse(source_path.read_text(encoding="utf-8")).body:
        targets: list[str] = []
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            targets.append(node.name)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets.append(node.target.id)
        elif isinstance(node, ast.Assign):
            targets.extend(t.id for t in node.targets if isinstance(t, ast.Name))
        names.update(name for name in targets if not name.startswith("_"))
    return frozenset(names)


class ContractsPackageBoundaryTests(unittest.TestCase):
    """The #1756 relocation: shim identity, facade, and import hygiene."""

    def test_core_shim_reexports_every_contract_name_with_identity_preserved(self) -> None:
        """``core.agent_runtime`` must stay a shim, not a drifting second copy.

        Identity — not just presence — is what keeps existing call sites
        correct: ``isinstance`` against a re-declared dataclass would fail for
        an instance built through the other import path.
        """

        contracts = importlib.import_module("telegram_bot.contracts.agent_runtime")
        shim = importlib.import_module("telegram_bot.core.agent_runtime")
        declared = _declared_public_names(_CONTRACTS_DIR / "agent_runtime.py")

        self.assertNotEqual(declared, frozenset())
        self.assertEqual(set(shim.__all__), set(declared))
        self.assertEqual(len(shim.__all__), len(set(shim.__all__)))
        for name in sorted(declared):
            with self.subTest(name=name):
                self.assertIs(getattr(shim, name), getattr(contracts, name))

        # The shim must not have grown a definition of its own.
        self.assertEqual(
            _declared_public_names(_BRIDGE_DIR / "core" / "agent_runtime.py"), frozenset()
        )

    def test_core_shim_instances_satisfy_isinstance_through_either_path(self) -> None:
        from telegram_bot.contracts.agent_runtime import TextDeltaEvent as ContractsTextDelta
        from telegram_bot.core.agent_runtime import TextDeltaEvent as ShimTextDelta

        self.assertIsInstance(ShimTextDelta(text="hi"), ContractsTextDelta)
        self.assertIsInstance(ContractsTextDelta(text="hi"), ShimTextDelta)

    def test_contracts_facade_exposes_the_agent_runtime_contract(self) -> None:
        facade = importlib.import_module("telegram_bot.contracts")
        contracts = importlib.import_module("telegram_bot.contracts.agent_runtime")
        declared = _declared_public_names(_CONTRACTS_DIR / "agent_runtime.py")

        self.assertEqual(set(facade.__all__), set(declared))
        for name in sorted(declared):
            with self.subTest(name=name):
                self.assertIs(getattr(facade, name), getattr(contracts, name))

        # CodexRuntime is a stable import path, deliberately not in the facade:
        # importing it costs the whole Codex app-server stack (see README.md).
        self.assertNotIn("CodexRuntime", facade.__all__)
        from telegram_bot.contracts.codex_runtime import CodexRuntime as FromContracts
        from telegram_bot.core.codex_runtime import CodexRuntime as FromCore

        self.assertIs(FromContracts, FromCore)

    def test_contracts_package_imports_without_telegram(self) -> None:
        """The seam must not re-couple every adapter to the Telegram runtime.

        Checked in a subprocess: this suite has already imported ``telegram``
        long before the assertion would run in-process, so only a fresh
        interpreter can observe what the contract imports actually pull in.
        """

        probe = textwrap.dedent(
            """
            import sys

            import telegram_bot.contracts
            import telegram_bot.contracts.agent_runtime
            import telegram_bot.contracts.codex_runtime

            leaked = sorted(
                name
                for name in sys.modules
                if name == "telegram" or name.startswith("telegram.")
            )
            print(",".join(leaked))
            """
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
        completed = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env=env,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "", "contracts imported the telegram package")

    def test_contracts_modules_declare_no_direct_telegram_imports(self) -> None:
        """Static backstop for the subprocess probe, including unused imports."""

        for source_path in sorted(_CONTRACTS_DIR.glob("*.py")):
            imported: set[str] = set()
            for node in ast.walk(ast.parse(source_path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    imported.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    imported.add(node.module)
            offenders = sorted(
                name for name in imported if name == "telegram" or name.startswith("telegram.")
            )
            with self.subTest(module=source_path.name):
                self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
