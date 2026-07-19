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
    MessageCompletedEvent,
    ModelInfo,
    ReasoningDeltaEvent,
    ResultEvent,
    SessionHistory,
    SessionHistoryMessage,
    SessionRequest,
    SessionSummary,
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
        self.supports_session_browsing = True
        self.session_summaries = (SessionSummary("thread-1", preview="hello"),)
        self.session_history = SessionHistory(
            "thread-1", (SessionHistoryMessage("assistant", "world"),)
        )

    async def start_or_resume(self, request: SessionRequest) -> FakeSession:
        self.requests.append(request)
        if self.sessions:
            return self.sessions.pop(0)
        return FakeSession(request.session_id or f"new-{len(self.requests)}")

    async def list_models(self):
        return (ModelInfo("codex-test", "Codex Test"),)

    async def list_sessions(self, *, limit: int = 10):
        return self.session_summaries[:limit]

    async def read_session(self, session_id: str, *, limit: int = 5):
        assert session_id == self.session_history.session_id
        return SessionHistory(session_id, self.session_history.messages[-limit:])

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
    assert settings.codex_cli_path == str(tmp_path / ".claude" / "hooks" / "ccc-codex")
    assert settings.codex_memory_materializer_path == str(
        tmp_path / ".claude" / "hooks" / "ccc_codex_memory.py"
    )
    codex_settings = settings_class.load(
        project_root=tmp_path / "project",
        environ={
            **environ,
            "CCC_AGENT_PROVIDER": "codex",
            "CCC_CODEX_CLI_PATH": "/opt/bin/codex-test",
            "CCC_CODEX_MEMORY_MATERIALIZER_PATH": "/opt/lib/ccc-materialize",
            "CCC_CODEX_MEMORY_BOOTSTRAP_TIMEOUT_SEC": "4.5",
        },
        bot_env_file=tmp_path / "missing.env",
    )
    assert codex_settings.agent_provider == "codex"
    assert codex_settings.codex_cli_path == "/opt/bin/codex-test"
    assert codex_settings.codex_memory_materializer_path == "/opt/lib/ccc-materialize"
    assert codex_settings.codex_memory_bootstrap_timeout_seconds == 4.5
    custom_harness = tmp_path / "custom-claude"
    custom_settings = settings_class.load(
        project_root=tmp_path / "project",
        environ={
            **environ,
            "CCC_CLAUDE_DIR": str(custom_harness),
        },
        bot_env_file=tmp_path / "missing.env",
    )
    assert custom_settings.codex_cli_path == str(custom_harness / "hooks" / "ccc-codex")
    assert custom_settings.codex_memory_materializer_path == str(
        custom_harness / "hooks" / "ccc_codex_memory.py"
    )
    assert custom_settings.claude_settings_path == custom_harness / "settings.json"
    assert custom_settings.logs_dir == tmp_path / "project" / ".telegram_bot" / "logs"
    assert custom_settings.session_store_path == (
        tmp_path / "project" / ".telegram_bot" / "sessions.json"
    )
    with pytest.raises(ValidationError, match="CCC_AGENT_PROVIDER|agent_provider"):
        settings_class.load(
            project_root=tmp_path / "project",
            environ={**environ, "CCC_AGENT_PROVIDER": "unknown"},
            bot_env_file=tmp_path / "missing.env",
        )


def test_claude_accepts_injected_runtime_for_staged_cutover(tmp_path: Path) -> None:
    # #584 slice B: Codex still REQUIRES an injected runtime; Claude ACCEPTS
    # one (the CCC_CLAUDE_RUNTIME_ADAPTER flag path). Without an injected
    # runtime the direct SDK path stays untouched.
    runtime = FakeRuntime()
    sdk_factory = object()

    handler = ProjectChatHandler(
        settings=_settings(tmp_path, provider="claude"),
        sdk_client_factory=cast(object, sdk_factory),
        agent_runtime=runtime,
    )

    assert handler._sdk_client_factory is sdk_factory
    assert handler._agent_runtime is runtime

    direct = ProjectChatHandler(
        settings=_settings(tmp_path, provider="claude"),
        sdk_client_factory=cast(object, sdk_factory),
    )
    assert direct._agent_runtime is None


def test_claude_flag_off_build_context_injects_no_runtime(tmp_path: Path) -> None:
    settings_class = _real_settings_class()
    _reload_real_module("telegram_bot.utils.chat_logger")
    _reload_real_module("telegram_bot.utils.health")
    from telegram_bot.__main__ import build_context

    settings = settings_class.load(
        project_root=tmp_path / "project",
        environ={"HOME": str(tmp_path), "TELEGRAM_BOT_TOKEN": "123456:test"},
        bot_env_file=tmp_path / "missing.env",
    )
    assert settings.agent_provider == "claude"
    assert settings.claude_runtime_adapter is False

    context = build_context(settings, sdk_factory=object(), telegram_port=lambda: None)

    # Flag off => zero behavior change: no runtime reaches ProjectChat and
    # process_message keeps dispatching to the direct Claude SDK path.
    assert context.agent_runtime is None
    assert context.project_chat._agent_runtime is None


def test_claude_flag_on_build_context_injects_claude_runtime(tmp_path: Path) -> None:
    settings_class = _real_settings_class()
    _reload_real_module("telegram_bot.utils.chat_logger")
    _reload_real_module("telegram_bot.utils.health")
    from telegram_bot.__main__ import build_context
    from telegram_bot.core.claude_runtime import ClaudeRuntime
    from telegram_bot.core.conversation_paths import claude_project_dir_name

    settings = settings_class.load(
        project_root=tmp_path / "project",
        environ={
            "HOME": str(tmp_path),
            "TELEGRAM_BOT_TOKEN": "123456:test",
            "CCC_CLAUDE_RUNTIME_ADAPTER": "true",
        },
        bot_env_file=tmp_path / "missing.env",
    )
    assert settings.claude_runtime_adapter is True

    context = build_context(settings, sdk_factory=object(), telegram_port=lambda: None)

    assert isinstance(context.agent_runtime, ClaudeRuntime)
    assert context.project_chat._agent_runtime is context.agent_runtime
    # Transcripts browsing resolves exactly like the direct path's
    # ProjectChatHandler.conversations_dir (~/.claude/projects/<project-dir>).
    expected_dir = (
        Path.home()
        / ".claude"
        / "projects"
        / claude_project_dir_name((tmp_path / "project").resolve())
    )
    assert context.agent_runtime._transcripts_dir == expected_dir
    assert context.project_chat.conversations_dir == expected_dir

    # An explicitly injected runtime is still respected under the flag.
    injected = FakeRuntime()
    override = build_context(
        settings,
        agent_runtime=injected,
        sdk_factory=object(),
        telegram_port=lambda: None,
    )
    assert override.agent_runtime is injected
    assert override.project_chat._agent_runtime is injected


@pytest.mark.anyio
async def test_claude_adapter_routes_turns_and_drops_codex_only_policy_knobs(
    tmp_path: Path,
) -> None:
    session = FakeSession("claude-session")
    runtime = FakeRuntime([session])
    handler = ProjectChatHandler(
        settings=_settings(tmp_path, provider="claude"), agent_runtime=runtime
    )
    handler._task_ledger_cache = False

    response = await handler.process_message(
        "hello",
        user_id=7,
        chat_id=70,
        approval_policy="never",
        approvals_reviewer=None,
        sandbox_policy={"type": "dangerFullAccess"},
    )

    # The turn flowed through the agent path, not the direct SDK path.
    assert session.messages == ["hello"]
    assert response.success is True
    assert response.content == "ok"
    assert response.session_id == "claude-session"
    # Codex app-server policy knobs (bot_access._codex_*) are not forwarded:
    # ClaudeRuntime fails closed on sandbox/reviewer policies and rejects
    # non-Claude permission modes.
    request = runtime.requests[0]
    assert request.approval_policy is None
    assert request.approvals_reviewer is None
    assert request.sandbox_policy is None


@pytest.mark.anyio
async def test_claude_adapter_approvals_use_the_generation_gated_callback(
    tmp_path: Path,
) -> None:
    approval = ApprovalRequestEvent("approval-1", "Bash", {"command": "ls"}, "run ls")
    session = FakeSession(
        "claude-approve",
        [approval, TextDeltaEvent("done"), CompletionEvent("end_turn")],
    )
    handler = ProjectChatHandler(
        settings=_settings(tmp_path, provider="claude"),
        agent_runtime=FakeRuntime([session]),
    )
    handler._task_ledger_cache = False
    seen: list[tuple[int, int, str, int]] = []

    async def approve(chat_id, user_id, event, generation):
        seen.append((chat_id, user_id, event.action, generation))
        return ApprovalDecision.ALLOW

    response = await handler.process_message(
        "go", user_id=7, chat_id=70, approval_callback=approve
    )

    assert session.approvals == [ApprovalDecision.ALLOW]
    assert len(seen) == 1
    assert seen[0][:3] == (70, 7, "Bash")
    assert handler.is_agent_approval_active(7, 70, seen[0][3]) is False
    assert response.content == "done"


@pytest.mark.anyio
async def test_claude_adapter_meters_request_and_result_tokens(tmp_path: Path) -> None:
    session = FakeSession(
        "claude-usage",
        [
            TextDeltaEvent("answer"),
            ResultEvent(
                {
                    "usage": {
                        "input_tokens": 10,
                        "cache_creation_input_tokens": 2,
                        "cache_read_input_tokens": 3,
                        "output_tokens": 4,
                    }
                }
            ),
            CompletionEvent("end_turn"),
        ],
    )
    handler = ProjectChatHandler(
        settings=_settings(tmp_path, provider="claude"),
        agent_runtime=FakeRuntime([session]),
    )
    handler._task_ledger_cache = False
    records: list[tuple[str, str, dict]] = []
    handler._usage_meter = SimpleNamespace(  # type: ignore[assignment]
        record=lambda provider, mode, **counts: records.append(
            (provider, mode, counts)
        )
    )

    response = await handler.process_message("meter", user_id=7, chat_id=70)

    assert response.success is True
    # One request at the spend boundary (first event), then the validated
    # input total (raw + cache creation + cache read) from the ResultEvent.
    assert records == [
        ("claude", "interactive", {"requests": 1}),
        ("claude", "interactive", {"input_tokens": 15, "output_tokens": 4}),
    ]


@pytest.mark.anyio
async def test_codex_adapter_path_never_uses_claude_metering(tmp_path: Path) -> None:
    session = FakeSession(
        "codex-usage",
        [
            TextDeltaEvent("answer"),
            ResultEvent({"usage": {"input_tokens": 10, "output_tokens": 4}}),
            CompletionEvent("end_turn"),
        ],
    )
    handler = _handler(tmp_path, FakeRuntime([session]))
    records: list[tuple[str, str, dict]] = []
    handler._usage_meter = SimpleNamespace(  # type: ignore[assignment]
        record=lambda provider, mode, **counts: records.append(
            (provider, mode, counts)
        )
    )

    response = await handler.process_message("meter", user_id=7, chat_id=70)

    # Codex meters at the runtime's own spend boundary (#388); the in-loop
    # Claude adapter metering must stay silent for it.
    assert response.success is True
    assert records == []


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


def test_codex_composition_wires_memory_bootstrap_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_class = _real_settings_class()
    _reload_real_module("telegram_bot.utils.chat_logger")
    _reload_real_module("telegram_bot.utils.health")
    from telegram_bot import __main__ as main_module
    from telegram_bot.core import codex_runtime as runtime_module

    settings = settings_class.load(
        project_root=tmp_path / "project",
        environ={
            "HOME": str(tmp_path),
            "TELEGRAM_BOT_TOKEN": "123456:test",
            "CCC_AGENT_PROVIDER": "codex",
        },
        bot_env_file=tmp_path / "missing.env",
    )
    captured: dict[str, object] = {}

    class Runtime:
        def __init__(
            self,
            *,
            cli_path,
            memory_materializer_path,
            memory_bootstrap_timeout_seconds,
        ) -> None:
            captured.update(
                cli_path=cli_path,
                memory_materializer_path=memory_materializer_path,
                memory_bootstrap_timeout_seconds=memory_bootstrap_timeout_seconds,
            )

    monkeypatch.setattr(runtime_module, "CodexRuntime", Runtime)
    context = main_module.build_context(
        settings,
        sdk_factory=object(),
        telegram_port=lambda: None,
    )

    assert context.agent_runtime is not None
    assert captured == {
        "cli_path": str(tmp_path / ".claude" / "hooks" / "ccc-codex"),
        "memory_materializer_path": str(tmp_path / ".claude" / "hooks" / "ccc_codex_memory.py"),
        "memory_bootstrap_timeout_seconds": 14.0,
    }


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
        "hello",
        user_id=7,
        chat_id=70,
        session_id="resumed",
        model="codex-test",
        effort="high",
        approval_policy="on-request",
        approvals_reviewer="auto_review",
        sandbox_policy={"type": "workspaceWrite", "networkAccess": False},
    )

    assert runtime.requests == [
        SessionRequest(
            working_directory=str(tmp_path.resolve()),
            session_id="resumed",
            model="codex-test",
            effort="high",
            approval_policy="on-request",
            approvals_reviewer="auto_review",
            sandbox_policy={"type": "workspaceWrite", "networkAccess": False},
        )
    ]
    assert session.messages == ["hello"]

    assert session.approvals == [ApprovalDecision.DENY]
    assert response.content == "hello world"
    assert "private chain" not in response.content
    assert response.success is True
    assert response.session_id == "resumed"


@pytest.mark.anyio
async def test_completed_message_before_tool_is_delivered_as_interim_bubble(
    tmp_path: Path,
) -> None:
    delivered: list[str] = []

    async def deliver_interim(content: str) -> None:
        delivered.append(content)

    session = FakeSession(
        "split",
        [
            TextDeltaEvent("Checking the repository now."),
            MessageCompletedEvent(),
            ToolStartedEvent("tool-1", "command", {"command": "gh issue list"}),
            ToolCompletedEvent("tool-1", "command", {"output": "17"}, True),
            TextDeltaEvent("There are 17 open issues."),
            MessageCompletedEvent(),
            ResultEvent({"status": "completed"}),
            CompletionEvent("end_turn"),
        ],
    )
    handler = _handler(tmp_path, FakeRuntime([session]))

    response = await handler.process_message(
        "How many issues are open?",
        user_id=7,
        chat_id=70,
        interim_message_callback=deliver_interim,
    )

    assert delivered == ["Checking the repository now."]
    assert response.content == "There are 17 open issues."
    assert response.streamed is False


@pytest.mark.anyio
async def test_terminal_completed_message_stays_on_final_delivery_path(tmp_path: Path) -> None:
    delivered: list[str] = []

    async def deliver_interim(content: str) -> None:
        delivered.append(content)

    session = FakeSession(
        "terminal",
        [
            TextDeltaEvent("The final answer."),
            MessageCompletedEvent(),
            ResultEvent({"status": "completed"}),
            CompletionEvent("end_turn"),
        ],
    )
    handler = _handler(tmp_path, FakeRuntime([session]))

    response = await handler.process_message(
        "answer",
        user_id=7,
        chat_id=70,
        interim_message_callback=deliver_interim,
    )

    assert delivered == []
    assert response.content == "The final answer."
    assert response.streamed is False


@pytest.mark.anyio
async def test_failed_interim_delivery_falls_back_to_complete_final_text(tmp_path: Path) -> None:
    async def fail_interim(_content: str) -> None:
        raise OSError("telegram unavailable")

    session = FakeSession(
        "fallback",
        [
            TextDeltaEvent("First message."),
            MessageCompletedEvent(),
            ToolStartedEvent("tool-1", "command", {"command": "true"}),
            ToolCompletedEvent("tool-1", "command", {"output": ""}, True),
            TextDeltaEvent("Final message."),
            MessageCompletedEvent(),
            CompletionEvent("end_turn"),
        ],
    )
    handler = _handler(tmp_path, FakeRuntime([session]))

    response = await handler.process_message(
        "go",
        user_id=7,
        chat_id=70,
        interim_message_callback=fail_interim,
    )

    assert response.content == "First message.\n\nFinal message."
    assert response.streamed is False


@pytest.mark.anyio
async def test_streaming_mode_finalizes_each_semantic_message_once(tmp_path: Path) -> None:
    class Bot:
        def __init__(self) -> None:
            self.sent: list[str] = []
            self.edited: list[str] = []

        async def send_message(self, *, chat_id, text, **_kwargs):
            del chat_id
            self.sent.append(text)
            return SimpleNamespace(message_id=len(self.sent))

        async def edit_message_text(self, *, chat_id, message_id, text, **_kwargs):
            del chat_id, message_id
            self.edited.append(text)
            return True

    session = FakeSession(
        "streaming-split",
        [
            TextDeltaEvent("Checking now."),
            MessageCompletedEvent(),
            ToolStartedEvent("tool-1", "command", {"command": "true"}),
            ToolCompletedEvent("tool-1", "command", {"output": ""}, True),
            TextDeltaEvent("Final answer."),
            MessageCompletedEvent(),
            CompletionEvent("end_turn"),
        ],
    )
    handler = _handler(tmp_path, FakeRuntime([session]))
    handler._config.enable_streaming = True
    handler._config.draft_update_min_chars = 20
    handler._config.draft_update_interval = 0.1
    handler._config.telegram_max_bubble_chars = 4000
    handler._config.enable_streaming_tool_calls = False
    bot = Bot()

    response = await handler.process_message(
        "go",
        user_id=7,
        chat_id=70,
        bot=bot,
    )

    assert bot.sent == ["Checking now.", "Final answer."]
    assert response.content == "Final answer."
    assert response.streamed is True


class ProgressSession(FakeSession):
    def __init__(self, session_id: str) -> None:
        super().__init__(session_id)
        self.tool_started = asyncio.Event()
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
            yield ToolStartedEvent(
                "tool-1",
                "commandExecution",
                {"command": "pwd"},
            )
            self.tool_started.set()
            await self.release.wait()
            yield ToolCompletedEvent(
                "tool-1",
                "commandExecution",
                {"exitCode": 0},
                True,
            )
            yield TextDeltaEvent("done")
            yield CompletionEvent("end_turn")

        return stream()


@pytest.mark.anyio
async def test_codex_keeps_typing_alive_and_shows_tool_heartbeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = ProgressSession("thread-1")
    handler = _handler(tmp_path, FakeRuntime([session]))
    handler._typing_interval_seconds = 0.01
    heartbeat_config = ProjectChatHandler._maybe_update_heartbeat.__globals__["config"]
    monkeypatch.setattr(heartbeat_config, "heartbeat_enabled", True)
    monkeypatch.setattr(heartbeat_config, "heartbeat_threshold_seconds", 0.01)
    monkeypatch.setattr(heartbeat_config, "heartbeat_update_interval_seconds", 0.01)
    monkeypatch.setattr(heartbeat_config, "heartbeat_stall_seconds", 0.0)
    monkeypatch.setattr(heartbeat_config, "heartbeat_forecast_enabled", False)

    typing_calls: list[str] = []
    status_calls: list[tuple[str | None, int | None]] = []
    status_visible = asyncio.Event()

    async def typing_callback() -> None:
        typing_calls.append("typing")

    async def status_callback(
        text: str | None, message_id: int | None = None
    ) -> int | None:
        status_calls.append((text, message_id))
        if text is None:
            return None
        status_visible.set()
        return message_id or 1234

    task = asyncio.create_task(
        handler.process_message(
            "run a command",
            user_id=7,
            chat_id=70,
            typing_callback=typing_callback,
            status_callback=status_callback,
        )
    )
    await asyncio.wait_for(session.tool_started.wait(), timeout=1)
    await asyncio.wait_for(status_visible.wait(), timeout=1)

    assert typing_calls
    assert any(
        text is not None
        and "⏳ Working" in text
        and "Command: pwd" in text
        for text, _ in status_calls
    )

    session.release.set()
    response = await asyncio.wait_for(task, timeout=1)

    assert response.content == "done"
    assert status_calls[-1] == (None, 1234)


@pytest.mark.anyio
async def test_codex_effort_change_recreates_wrapper_and_resumes_same_thread(
    tmp_path: Path,
) -> None:
    low_session = FakeSession("thread-1")
    high_session = FakeSession("thread-1")
    runtime = FakeRuntime([low_session, high_session])
    handler = _handler(tmp_path, runtime)

    first = await handler.process_message(
        "first", user_id=7, chat_id=70, session_id="thread-1", effort="low"
    )
    second = await handler.process_message(
        "second", user_id=7, chat_id=70, session_id=first.session_id, effort="high"
    )

    assert [request.effort for request in runtime.requests] == ["low", "high"]
    assert [request.session_id for request in runtime.requests] == ["thread-1", "thread-1"]
    assert low_session.messages == ["first"]
    assert high_session.messages == ["second"]
    assert second.session_id == "thread-1"


@pytest.mark.anyio
async def test_codex_reviewer_or_sandbox_change_recreates_wrapper(
    tmp_path: Path,
) -> None:
    sessions = [FakeSession("thread-1") for _ in range(3)]
    runtime = FakeRuntime(sessions)
    handler = _handler(tmp_path, runtime)

    common = {
        "user_id": 7,
        "chat_id": 70,
        "session_id": "thread-1",
        "approval_policy": "on-request",
    }
    await handler.process_message(
        "first",
        **common,
        approvals_reviewer="user",
        sandbox_policy={"type": "workspaceWrite", "networkAccess": False},
    )
    await handler.process_message(
        "second",
        **common,
        approvals_reviewer="auto_review",
        sandbox_policy={"type": "workspaceWrite", "networkAccess": False},
    )
    await handler.process_message(
        "third",
        **common,
        approvals_reviewer="auto_review",
        sandbox_policy={"type": "workspaceWrite", "networkAccess": True},
    )

    assert len(runtime.requests) == 3
    assert [request.approvals_reviewer for request in runtime.requests] == [
        "user",
        "auto_review",
        "auto_review",
    ]
    assert [request.sandbox_policy["networkAccess"] for request in runtime.requests] == [
        False,
        False,
        True,
    ]


@pytest.mark.anyio
async def test_codex_uses_only_explicit_provider_neutral_approval_callback(
    tmp_path: Path,
) -> None:
    approval = ApprovalRequestEvent("approval-1", "write_file", {"path": "secret"}, "write")
    denied = FakeSession("denied", [approval, CompletionEvent("end_turn")])
    allowed = FakeSession("allowed", [approval, CompletionEvent("end_turn")])
    handler = _handler(tmp_path, FakeRuntime([denied, allowed]))
    seen: list[tuple[int, int, str, int]] = []

    async def approve(chat_id, user_id, event, generation):
        seen.append((chat_id, user_id, event.request_id, generation))
        return ApprovalDecision.ALLOW

    await handler.process_message("default", 7, 70)
    await handler.process_message(
        "explicit",
        7,
        71,
        approval_callback=approve,
    )

    assert denied.approvals == [ApprovalDecision.DENY]
    assert allowed.approvals == [ApprovalDecision.ALLOW]
    assert len(seen) == 1
    assert seen[0][:3] == (71, 7, "approval-1")
    assert handler.is_agent_approval_active(7, 71, seen[0][3]) is False


@pytest.mark.anyio
async def test_codex_browsing_methods_delegate_without_claude_transcript_access(
    tmp_path: Path,
) -> None:
    runtime = FakeRuntime()
    handler = _handler(tmp_path, runtime)

    sessions = await handler.list_runtime_sessions(limit=1)
    history = await handler.read_runtime_session("thread-1", limit=1)
    models = await handler.list_runtime_models()

    assert sessions == runtime.session_summaries
    assert history == runtime.session_history
    assert [(model.id, model.display_name) for model in models] == [
        ("codex-test", "Codex Test")
    ]


@pytest.mark.anyio
async def test_runtime_browsing_fails_safely_when_capability_is_unavailable(
    tmp_path: Path,
) -> None:
    runtime = FakeRuntime()
    runtime.supports_session_browsing = False
    handler = _handler(tmp_path, runtime)

    with pytest.raises(RuntimeError, match="unavailable"):
        await handler.list_runtime_sessions()
    with pytest.raises(RuntimeError, match="unavailable"):
        await handler.read_runtime_session("thread-1")


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


class StallSession(FakeSession):
    """Yields some events, then the terminal event never arrives (#411 C)."""

    def __init__(self, session_id: str, pre_events: list[AgentEvent] | None = None) -> None:
        super().__init__(session_id)
        self.pre_events = (
            pre_events if pre_events is not None else [TextDeltaEvent("partial answer")]
        )
        self.hang = asyncio.Event()
        self.closed = False

    def send_turn(
        self,
        message: str,
        *,
        approval_handler: ApprovalHandler = deny_approval,
    ) -> AsyncIterator[AgentEvent]:
        del approval_handler

        async def stream() -> AsyncIterator[AgentEvent]:
            self.messages.append(message)
            try:
                for event in self.pre_events:
                    yield event
                await self.hang.wait()
            finally:
                self.closed = True

        return stream()


def _stall_handler(
    tmp_path: Path, runtime: FakeRuntime, monkeypatch: pytest.MonkeyPatch
) -> tuple[ProjectChatHandler, list[int]]:
    stalled: list[int] = []
    # Patch the exact globals the method executes with: sibling test modules
    # rebuild telegram_bot.core.* sys.modules entries at import time, so a
    # dotted-name patch can hit a different module generation.
    process_globals = ProjectChatHandler._process_agent_message.__globals__
    monkeypatch.setitem(
        process_globals,
        "health_reporter",
        SimpleNamespace(
            record_claude_error=lambda *a, **k: None,
            record_claude_ok=lambda *a, **k: None,
            record_stalled_request=lambda count=1: stalled.append(count),
        ),
    )
    settings = _settings(tmp_path)
    settings.terminal_stall_seconds = 0.05
    handler = ProjectChatHandler(settings=settings, agent_runtime=runtime)
    handler._task_ledger_cache = False
    return handler, stalled


@pytest.mark.anyio
async def test_codex_terminal_stall_releases_turn_and_queued_request_proceeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RED for #411 C: text arrives, the completion event vanishes; the turn
    must terminalize within the bounded grace and free the conversation."""

    stall = StallSession("thread-1")
    follow = FakeSession("thread-1")
    runtime = FakeRuntime([stall, follow])
    handler, stalled = _stall_handler(tmp_path, runtime, monkeypatch)

    first_task = asyncio.create_task(handler.process_message("hang", 7, 70))
    await _wait_until(lambda: stall.messages == ["hang"])
    queued_task = asyncio.create_task(handler.process_message("queued", 7, 70))

    first, queued = await asyncio.wait_for(
        asyncio.gather(first_task, queued_task), timeout=5
    )

    assert first.success is True
    assert "partial answer" in first.content
    assert "closed automatically" in first.content
    # The turn is interrupted and its abandoned generator is closed, so a late
    # completion event has no consumer left — the answer cannot deliver twice.
    assert stall.interrupt_calls == 1
    assert stall.closed is True
    assert stalled == [1]
    # The queued follow-up ran after the release on a fresh session.
    assert queued.success is True and queued.content == "ok"
    assert follow.messages == ["queued"]
    assert len(runtime.requests) == 2


@pytest.mark.anyio
async def test_codex_no_stall_release_without_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stall = StallSession("thread-1", pre_events=[])
    handler, stalled = _stall_handler(tmp_path, FakeRuntime([stall]), monkeypatch)
    handler._process_timeout_seconds = 0.2

    response = await asyncio.wait_for(handler.process_message("hang", 7, 70), timeout=5)

    assert response.success is False
    assert response.error is not None and "Timed out" in response.error
    assert stalled == []


@pytest.mark.anyio
async def test_codex_no_stall_release_while_tool_is_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stall = StallSession(
        "thread-1",
        pre_events=[
            TextDeltaEvent("working"),
            ToolStartedEvent("tool-1", "command", {"command": "sleep"}),
        ],
    )
    handler, stalled = _stall_handler(tmp_path, FakeRuntime([stall]), monkeypatch)
    handler._process_timeout_seconds = 0.2

    response = await asyncio.wait_for(handler.process_message("hang", 7, 70), timeout=5)

    assert response.success is False
    assert response.error is not None and "Timed out" in response.error
    assert stalled == []


@pytest.mark.anyio
async def test_codex_no_stall_release_while_approval_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stall = StallSession(
        "thread-1",
        pre_events=[
            TextDeltaEvent("needs approval"),
            ApprovalRequestEvent("approval-1", "write_file", {"path": "x"}, "write x"),
        ],
    )
    handler, stalled = _stall_handler(tmp_path, FakeRuntime([stall]), monkeypatch)
    handler._process_timeout_seconds = 0.2

    response = await asyncio.wait_for(handler.process_message("hang", 7, 70), timeout=5)

    assert response.success is False
    assert response.error is not None and "Timed out" in response.error
    assert stalled == []


@pytest.mark.anyio
async def test_codex_completed_turn_after_tool_is_not_stalled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A normal tool→text→completion turn must be untouched by the guard."""

    session = FakeSession(
        "thread-1",
        [
            ToolStartedEvent("tool-1", "command", {"command": "pwd"}),
            ToolCompletedEvent("tool-1", "command", {"output": "/"}, True),
            TextDeltaEvent("done"),
            CompletionEvent("end_turn"),
        ],
    )
    handler, stalled = _stall_handler(tmp_path, FakeRuntime([session]), monkeypatch)

    response = await asyncio.wait_for(handler.process_message("go", 7, 70), timeout=5)

    assert response.success is True
    assert response.content == "done"
    assert stalled == []


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


@pytest.mark.anyio
async def test_codex_streaming_cancel_failure_never_masks_primary_outcome(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    class BrokenStreamingHandler:
        async def cancel(self) -> None:
            raise RuntimeError("telegram cancel failed")

    handler = _handler(tmp_path, FakeRuntime())

    await handler._cancel_agent_streaming(
        BrokenStreamingHandler(), context="test primary outcome"
    )

    assert "test primary outcome" in caplog.text
    assert "telegram cancel failed" in caplog.text


class _RecordingBot:
    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[tuple[int, str]] = []
        self.fail = fail

    async def send_message(self, *, chat_id: int, text: str, **_kwargs: object) -> None:
        if self.fail:
            raise RuntimeError("telegram send failed")
        self.sent.append((chat_id, text))


class UnsolicitedFakeSession(FakeSession):
    """FakeSession exposing the optional between-turns delivery seam."""

    def __init__(self, session_id: str, events: list[AgentEvent] | None = None) -> None:
        super().__init__(session_id, events)
        self.unsolicited_handler = None

    def set_unsolicited_handler(self, handler) -> None:
        self.unsolicited_handler = handler


@pytest.mark.anyio
async def test_unsolicited_seam_registers_and_delivers_through_the_bot(
    tmp_path: Path,
) -> None:
    # #584 P3-1B: on the adapter path, autonomous output produced between
    # turns must reach the same conversation via the bot route the direct
    # path's deliver_unsolicited uses (notification_bot preferred over bot).
    session = UnsolicitedFakeSession("claude-bg")
    handler = _handler(tmp_path, FakeRuntime([session]))
    bot = _RecordingBot()
    notification_bot = _RecordingBot()

    response = await handler.process_message(
        "hello", 7, 70, bot=bot, notification_bot=notification_bot
    )

    assert response.success is True
    assert session.unsolicited_handler is not None

    await session.unsolicited_handler("background report", "claude-bg")
    assert notification_bot.sent == [(70, "background report")]
    assert bot.sent == []

    # Empty runtime text mirrors the direct path's "(No response)" fallback.
    await session.unsolicited_handler("", None)
    assert notification_bot.sent[-1] == (70, "(No response)")

    # Oversized results are bounded exactly like the direct path.
    await session.unsolicited_handler("x" * 5000, "claude-bg")
    chat_id, text = notification_bot.sent[-1]
    assert chat_id == 70
    assert len(text) <= 4000
    assert text.endswith("… (background result truncated)")


@pytest.mark.anyio
async def test_unsolicited_registration_skipped_without_bot_and_kept_across_turns(
    tmp_path: Path,
) -> None:
    session = UnsolicitedFakeSession("claude-bg")
    session.events = [TextDeltaEvent("ok"), CompletionEvent("end_turn")]
    handler = _handler(tmp_path, FakeRuntime([session]))

    # No bot route on this turn: nothing is registered.
    await handler.process_message("first", 7, 70)
    assert session.unsolicited_handler is None

    # A later turn carrying a bot registers the route on the cached session;
    # a bot-less turn afterwards keeps (does not clear) the registration.
    bot = _RecordingBot()
    await handler.process_message("second", 7, 70, bot=bot)
    registered = session.unsolicited_handler
    assert registered is not None
    await handler.process_message("third", 7, 70)
    assert session.unsolicited_handler is registered

    await registered("late background answer", "claude-bg")
    assert bot.sent == [(70, "late background answer")]


@pytest.mark.anyio
async def test_unsolicited_delivery_failure_is_contained(tmp_path: Path) -> None:
    session = UnsolicitedFakeSession("claude-bg")
    handler = _handler(tmp_path, FakeRuntime([session]))
    bot = _RecordingBot(fail=True)

    await handler.process_message("hello", 7, 70, bot=bot)
    assert session.unsolicited_handler is not None

    # A broken Telegram route must not raise back into the runtime reader.
    await session.unsolicited_handler("background report", "claude-bg")
    assert bot.sent == []


@pytest.mark.anyio
async def test_sessions_without_unsolicited_seam_stay_untouched(tmp_path: Path) -> None:
    # Codex sessions expose no set_unsolicited_handler; the optional seam
    # must be probed with getattr and absence must change nothing.
    session = FakeSession("codex-plain")
    handler = _handler(tmp_path, FakeRuntime([session]))

    response = await handler.process_message("hello", 7, 70, bot=_RecordingBot())

    assert response.success is True
    assert not hasattr(session, "unsolicited_handler")
