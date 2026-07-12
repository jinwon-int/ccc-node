"""Deterministic ProjectChat coverage for the provider-neutral runtime path."""

from __future__ import annotations

import asyncio
import importlib
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from pydantic import ValidationError

from telegram_bot.core.agent_runtime import (
    AgentEvent,
    ApprovalDecision,
    ApprovalHandler,
    ApprovalRequestEvent,
    CompletionEvent,
    ErrorEvent,
    ReasoningDeltaEvent,
    ResultEvent,
    SessionRequest,
    TextDeltaEvent,
    ToolCompletedEvent,
    ToolStartedEvent,
    deny_approval,
)
from telegram_bot.core.project_chat import ProjectChatHandler


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _real_settings_class():
    sys.modules.pop("telegram_bot.utils.config", None)
    return importlib.import_module("telegram_bot.utils.config").Settings


def _reload_real_module(name: str):
    sys.modules.pop(name, None)
    return importlib.import_module(name)


def _settings(tmp_path: Path, provider: str = "codex") -> SimpleNamespace:
    return SimpleNamespace(
        agent_provider=provider,
        project_root=tmp_path,
        execution_profile="strict-project",
        bash_policy="disabled",
        allowed_user_ids=[7],
        require_allowlist=True,
        claude_cli_path=None,
        enable_streaming=False,
        enable_partial_streaming=False,
        bot_data_dir=None,
        task_ledger_path=None,
    )


class FakeSession:
    def __init__(self, session_id: str, events: list[AgentEvent] | None = None) -> None:
        self.session_id = session_id
        self.events = events or [TextDeltaEvent("ok"), CompletionEvent("end_turn")]
        self.messages: list[str] = []
        self.approvals: list[ApprovalDecision] = []
        self.interrupt_calls = 0

    def send_turn(
        self,
        message: str,
        *,
        approval_handler: ApprovalHandler = deny_approval,
    ) -> AsyncIterator[AgentEvent]:
        async def stream() -> AsyncIterator[AgentEvent]:
            self.messages.append(message)
            for event in self.events:
                if isinstance(event, ApprovalRequestEvent):
                    self.approvals.append(await approval_handler(event))
                yield event

        return stream()

    async def interrupt(self) -> None:
        self.interrupt_calls += 1


class FakeRuntime:
    def __init__(self, sessions: list[FakeSession] | None = None) -> None:
        self.sessions = sessions or []
        self.requests: list[SessionRequest] = []
        self.close_calls = 0

    async def start_or_resume(self, request: SessionRequest) -> FakeSession:
        self.requests.append(request)
        if self.sessions:
            return self.sessions.pop(0)
        return FakeSession(request.session_id or f"new-{len(self.requests)}")

    async def list_models(self):
        return ()

    async def close(self) -> None:
        self.close_calls += 1


def _handler(tmp_path: Path, runtime: FakeRuntime) -> ProjectChatHandler:
    handler = ProjectChatHandler(settings=_settings(tmp_path), agent_runtime=runtime)
    handler._task_ledger_cache = False
    return handler


async def _wait_until(predicate, *, timeout: float = 1.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0)


def test_agent_provider_settings_default_and_reject_unknown(tmp_path: Path) -> None:
    settings_class = _real_settings_class()
    environ = {"HOME": str(tmp_path), "TELEGRAM_BOT_TOKEN": "123456:test"}

    settings = settings_class.load(
        project_root=tmp_path / "project",
        environ=environ,
        bot_env_file=tmp_path / "missing.env",
    )

    assert settings.agent_provider == "claude"
    assert settings.codex_cli_path == "codex"
    codex_settings = settings_class.load(
        project_root=tmp_path / "project",
        environ={
            **environ,
            "CCC_AGENT_PROVIDER": "codex",
            "CCC_CODEX_CLI_PATH": "/opt/bin/codex-test",
        },
        bot_env_file=tmp_path / "missing.env",
    )
    assert codex_settings.agent_provider == "codex"
    assert codex_settings.codex_cli_path == "/opt/bin/codex-test"
    with pytest.raises(ValidationError, match="CCC_AGENT_PROVIDER|agent_provider"):
        settings_class.load(
            project_root=tmp_path / "project",
            environ={**environ, "CCC_AGENT_PROVIDER": "unknown"},
            bot_env_file=tmp_path / "missing.env",
        )


def test_claude_default_ignores_provider_runtime(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    sdk_factory = object()

    handler = ProjectChatHandler(
        settings=_settings(tmp_path, provider="claude"),
        sdk_client_factory=cast(object, sdk_factory),
        agent_runtime=runtime,
    )

    assert handler._sdk_client_factory is sdk_factory
    assert handler._agent_runtime is None


def test_codex_composition_injects_runtime_without_replacing_sdk_factory(tmp_path: Path) -> None:
    settings_class = _real_settings_class()
    _reload_real_module("telegram_bot.utils.chat_logger")
    _reload_real_module("telegram_bot.utils.health")
    from telegram_bot.__main__ import build_context

    settings = settings_class.load(
        project_root=tmp_path / "project",
        environ={
            "HOME": str(tmp_path),
            "TELEGRAM_BOT_TOKEN": "123456:test",
            "CCC_AGENT_PROVIDER": "codex",
        },
        bot_env_file=tmp_path / "missing.env",
    )
    runtime = FakeRuntime()
    sdk_factory = object()

    context = build_context(
        settings,
        agent_runtime=runtime,
        sdk_factory=sdk_factory,
        telegram_port=lambda: None,
    )

    assert context.agent_runtime is runtime
    assert context.project_chat._agent_runtime is runtime
    assert context.project_chat._sdk_client_factory is sdk_factory


def test_codex_runtime_wires_configured_cli_path(monkeypatch: pytest.MonkeyPatch) -> None:
    from telegram_bot.core import codex_runtime as runtime_module

    captured: dict[str, object] = {}

    class Client:
        def __init__(self, *, executable, server_request_handler):
            captured["executable"] = executable
            captured["handler"] = server_request_handler

    monkeypatch.setattr(runtime_module, "CodexAppServerClient", Client)

    runtime_module.CodexRuntime(cli_path="/opt/bin/codex-test")

    assert captured["executable"] == "/opt/bin/codex-test"
    assert callable(captured["handler"])


@pytest.mark.anyio
async def test_codex_start_resume_and_event_mapping_hides_reasoning(tmp_path: Path) -> None:
    approval = ApprovalRequestEvent("approval-1", "write_file", {"path": "x"}, "write x")
    session = FakeSession(
        "resumed",
        [
            TextDeltaEvent("hello"),
            ReasoningDeltaEvent("private chain"),
            ToolStartedEvent("tool-1", "command", {"command": "pwd"}),
            ToolCompletedEvent("tool-1", "command", {"output": str(tmp_path)}, True),
            approval,
            TextDeltaEvent(" world"),
            ResultEvent({"status": "completed"}),
            CompletionEvent("end_turn"),
        ],
    )
    runtime = FakeRuntime([session])
    handler = _handler(tmp_path, runtime)

    response = await handler.process_message(
        "hello", user_id=7, chat_id=70, session_id="resumed", model="codex-test"
    )

    assert runtime.requests == [
        SessionRequest(working_directory=str(tmp_path.resolve()), session_id="resumed", model="codex-test")
    ]
    assert session.messages == ["hello"]
    assert session.approvals == [ApprovalDecision.DENY]
    assert response.content == "hello world"
    assert "private chain" not in response.content
    assert response.success is True
    assert response.session_id == "resumed"


class BlockingSession(FakeSession):
    def __init__(self, session_id: str) -> None:
        super().__init__(session_id)
        self.started: dict[str, asyncio.Event] = {}
        self.release: dict[str, asyncio.Event] = {}

    def send_turn(
        self,
        message: str,
        *,
        approval_handler: ApprovalHandler = deny_approval,
    ) -> AsyncIterator[AgentEvent]:
        del approval_handler

        async def stream() -> AsyncIterator[AgentEvent]:
            self.messages.append(message)
            self.started.setdefault(message, asyncio.Event()).set()
            await self.release.setdefault(message, asyncio.Event()).wait()
            yield TextDeltaEvent(message)
            yield CompletionEvent("end_turn")

        return stream()


@pytest.mark.anyio
async def test_codex_serializes_same_conversation_but_isolates_other_chats(tmp_path: Path) -> None:
    first = BlockingSession("thread-a")
    other = BlockingSession("thread-b")
    runtime = FakeRuntime([first, other])
    handler = _handler(tmp_path, runtime)

    first_task = asyncio.create_task(handler.process_message("first", 7, 70))
    await _wait_until(lambda: "first" in first.started)
    await first.started["first"].wait()
    queued_task = asyncio.create_task(handler.process_message("queued", 7, 70))
    other_task = asyncio.create_task(handler.process_message("other", 7, 71))
    await _wait_until(lambda: "other" in other.started)
    await other.started["other"].wait()
    await asyncio.sleep(0)

    assert first.messages == ["first"]
    assert other.messages == ["other"]
    first.release["first"].set()
    await _wait_until(lambda: "queued" in first.started)
    first.release["queued"].set()
    other.release["other"].set()
    responses = await asyncio.gather(first_task, queued_task, other_task)
    assert [response.content for response in responses] == ["first", "queued", "other"]


@pytest.mark.anyio
async def test_codex_stop_interrupts_only_exact_active_conversation(tmp_path: Path) -> None:
    first = BlockingSession("thread-a")
    other = BlockingSession("thread-b")
    handler = _handler(tmp_path, FakeRuntime([first, other]))
    first_task = asyncio.create_task(handler.process_message("first", 7, 70))
    other_task = asyncio.create_task(handler.process_message("other", 7, 71))
    await _wait_until(lambda: "first" in first.started and "other" in other.started)

    assert await handler.stop(7, chat_id=70) is True
    assert first.interrupt_calls == 1
    assert other.interrupt_calls == 0

    first.release["first"].set()
    other.release["other"].set()
    await asyncio.gather(first_task, other_task)


@pytest.mark.anyio
async def test_codex_error_cleans_session_and_next_turn_restarts(tmp_path: Path) -> None:
    failed = FakeSession("failed", [ErrorEvent("failed", "provider failed")])
    recovered = FakeSession("recovered")
    runtime = FakeRuntime([failed, recovered])
    handler = _handler(tmp_path, runtime)

    response = await handler.process_message("fail", 7, 70)
    retry = await handler.process_message("retry", 7, 70)

    assert response.success is False
    assert response.error == "provider failed"
    assert retry.success is True
    assert len(runtime.requests) == 2


@pytest.mark.anyio
async def test_codex_clear_and_close_are_bounded_and_idempotent(tmp_path: Path) -> None:
    session = BlockingSession("thread-a")
    runtime = FakeRuntime([session])
    handler = _handler(tmp_path, runtime)
    task = asyncio.create_task(handler.process_message("first", 7, 70))
    await _wait_until(lambda: "first" in session.started)

    await handler.clear_user_stream(7, chat_id=70)
    await handler.close()
    await handler.close()

    assert session.interrupt_calls == 1
    assert runtime.close_calls == 1
    session.release["first"].set()
    await task


class HangingSession(FakeSession):
    def __init__(self, session_id: str) -> None:
        super().__init__(session_id)
        self.release = asyncio.Event()

    def send_turn(
        self,
        message: str,
        *,
        approval_handler: ApprovalHandler = deny_approval,
    ) -> AsyncIterator[AgentEvent]:
        del approval_handler

        async def stream() -> AsyncIterator[AgentEvent]:
            self.messages.append(message)
            await self.release.wait()
            yield TextDeltaEvent("late")

        return stream()


@pytest.mark.anyio
async def test_codex_turn_timeout_interrupts_and_cleans_session(tmp_path: Path) -> None:
    hung = HangingSession("hung")
    recovered = FakeSession("recovered")
    runtime = FakeRuntime([hung, recovered])
    handler = _handler(tmp_path, runtime)
    handler._process_timeout_seconds = 0.01

    response = await asyncio.wait_for(handler.process_message("hang", 7, 70), timeout=1.0)
    retry = await handler.process_message("retry", 7, 70)

    assert response.success is False
    assert response.error is not None and "Timed out" in response.error
    assert hung.interrupt_calls == 1
    assert retry.success is True
    assert len(runtime.requests) == 2


class HangingInterruptSession(BlockingSession):
    async def interrupt(self) -> None:
        self.interrupt_calls += 1
        await asyncio.Event().wait()


@pytest.mark.anyio
async def test_codex_close_bounds_hung_interrupt_and_still_closes_runtime(tmp_path: Path) -> None:
    session = HangingInterruptSession("thread-a")
    runtime = FakeRuntime([session])
    handler = _handler(tmp_path, runtime)
    handler._agent_interrupt_timeout_seconds = 0.01
    task = asyncio.create_task(handler.process_message("first", 7, 70))
    await _wait_until(lambda: "first" in session.started)

    await asyncio.wait_for(handler.close(), timeout=1.0)

    assert session.interrupt_calls == 1
    assert runtime.close_calls == 1
    session.release["first"].set()
    await task
