"""Provider-neutral runtime adapter for Piri's headless RPC protocol."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat
from types import MappingProxyType
from typing import Any, Protocol, cast

from .agent_runtime import (
    AgentEvent,
    ApprovalHandler,
    CompletionEvent,
    ErrorEvent,
    MessageCompletedEvent,
    ModelInfo,
    ReasoningDeltaEvent,
    ResultEvent,
    SessionRequest,
    TextDeltaEvent,
    ToolCompletedEvent,
    ToolStartedEvent,
    deny_approval,
)
from .piri_rpc import PiriRpcProcessClient
from .codex_runtime import _run_codex_memory_bootstrap
from telegram_bot.memory.distill_types import (
    CodexTranscriptSnapshot,
    TranscriptBounds,
)
from telegram_bot.memory.piri_snapshot import (
    find_piri_session_directory,
    read_piri_snapshot,
)
from telegram_bot.utils.secure_fs import ensure_private_directory


_REASONING_LEVELS = ("minimal", "low", "medium", "high", "xhigh", "max")
_FULL_ACCESS_SANDBOX_TYPES = frozenset({"dangerFullAccess", "danger-full-access"})
_TURN_CLEANUP_TIMEOUT_SECONDS = 5.0
_SESSION_ID_PATTERN = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?$"
)
_MODEL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,255}$")


class PiriClient(Protocol):
    async def start(self) -> None: ...

    async def prompt(self, message: str) -> None: ...

    async def abort(self) -> None: ...

    async def get_state(self) -> Mapping[str, Any]: ...

    async def get_available_models(self) -> Sequence[Mapping[str, Any]]: ...

    async def next_event(self) -> Mapping[str, Any]: ...

    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class PiriLaunchConfig:
    """Auditable process boundary for one unrestricted Piri session."""

    command: tuple[str, ...]
    working_directory: str
    environment: Mapping[str, str]
    auto_confirm_extensions: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "environment", MappingProxyType(dict(self.environment)))


PiriClientFactory = Callable[[PiriLaunchConfig], PiriClient]
PiriMemoryEnvironmentValidator = Callable[[Mapping[str, str]], object]
PiriRouteEnvironmentFactory = Callable[[str, str], Mapping[str, str]]


class PiriSession:
    """One serialized ccc-node session backed by one persistent Piri process."""

    def __init__(
        self,
        session_id: str,
        client: PiriClient,
        *,
        on_close: Callable[[PiriSession], None] | None = None,
    ) -> None:
        if not session_id:
            raise ValueError("Piri session id must not be empty")
        self._session_id = session_id
        self._client = client
        self._turn_lock = asyncio.Lock()
        self._active = False
        self._interrupt_requested = False
        self._on_close = on_close
        self._closed = False

    @property
    def session_id(self) -> str:
        return self._session_id

    async def interrupt(self) -> None:
        if not self._active:
            return
        self._interrupt_requested = True
        await self._client.abort()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._client.close()
        finally:
            if self._on_close is not None:
                self._on_close(self)

    async def _send_turn(
        self,
        message: str,
        *,
        approval_handler: ApprovalHandler,
    ) -> AsyncIterator[AgentEvent]:
        del approval_handler  # Piri built-ins execute directly and never request approval.
        if not message:
            yield ErrorEvent(code="invalid_prompt", message="Piri prompt must not be empty")
            return

        async with self._turn_lock:
            self._active = True
            self._interrupt_requested = False
            state = _TurnState()
            turn_settled = False
            cleanup_required = False
            try:
                await self._client.prompt(message)
                cleanup_required = True
                while True:
                    event = await self._client.next_event()
                    if event.get("type") == "agent_settled":
                        # Mark settled before yielding either terminal sequence. If the
                        # consumer closes between ResultEvent and CompletionEvent, there
                        # is no provider work left to abort or drain.
                        turn_settled = True
                        for item in _terminal_events(
                            state,
                            session_id=self._session_id,
                            interrupted=self._interrupt_requested,
                        ):
                            yield item
                        return
                    for item in _normalized_events(event, state):
                        yield item
            except asyncio.CancelledError:
                cleanup_required = True
                raise
            except Exception:
                # Provider/transport details may contain credentials or private paths.
                if self._interrupt_requested:
                    yield ErrorEvent(
                        code="interrupted",
                        message="Piri turn was interrupted",
                    )
                else:
                    yield ErrorEvent(
                        code="piri_runtime_error",
                        message="Piri runtime failed",
                        retryable=False,
                    )
            finally:
                try:
                    if not turn_settled and (cleanup_required or self._interrupt_requested):
                        await self._abort_and_drain()
                finally:
                    self._active = False
                    self._interrupt_requested = False

    def send_turn(
        self,
        message: str,
        *,
        approval_handler: ApprovalHandler = deny_approval,
    ) -> AsyncIterator[AgentEvent]:
        return self._send_turn(message, approval_handler=approval_handler)

    async def _abort_and_drain(self) -> None:
        """Leave the persistent process at a clean turn boundary after early exit."""

        try:
            if not self._interrupt_requested:
                await asyncio.wait_for(
                    self._client.abort(),
                    timeout=_TURN_CLEANUP_TIMEOUT_SECONDS,
                )
            while True:
                event = await asyncio.wait_for(
                    self._client.next_event(),
                    timeout=_TURN_CLEANUP_TIMEOUT_SECONDS,
                )
                if event.get("type") == "agent_settled":
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            # A process that cannot prove it settled is unsafe to reuse.
            await self._client.close()


class PiriRuntime:
    """Create unrestricted Piri sessions with project resources pre-trusted.

    This adapter deliberately has no command-approval surface.  Piri receives
    the host user's OS permissions, all default tools remain enabled, and
    project-local resources are trusted with ``--approve``.  Restrictive ccc
    policies are rejected instead of being silently ignored.
    """

    def __init__(
        self,
        *,
        executable: str = "piri",
        client_factory: PiriClientFactory | None = None,
        process_environment: Mapping[str, str] | None = None,
        model_catalog_directory: str | None = None,
        auto_confirm_extensions: bool = True,
        memory_materializer_path: str | None = None,
        memory_bootstrap_timeout_seconds: float = 14.0,
        memory_environment_validator: PiriMemoryEnvironmentValidator | None = None,
        route_environment_factory: PiriRouteEnvironmentFactory | None = None,
    ) -> None:
        if not executable.strip():
            raise ValueError("Piri executable must not be empty")
        if model_catalog_directory is not None and not model_catalog_directory:
            raise ValueError("Piri model catalog directory must not be empty")
        if memory_materializer_path is not None and not memory_materializer_path.strip():
            raise ValueError("Piri memory materializer path must not be empty")
        if memory_bootstrap_timeout_seconds <= 0 or memory_bootstrap_timeout_seconds > 30:
            raise ValueError("Piri memory bootstrap timeout is invalid")
        if process_environment is not None:
            for name, value in process_environment.items():
                if not isinstance(name, str) or not name or "\x00" in name:
                    raise ValueError("Piri process environment name is invalid")
                if not isinstance(value, str) or "\x00" in value:
                    raise ValueError("Piri process environment value is invalid")
        self._executable = executable
        self._process_environment = (
            dict(process_environment) if process_environment is not None else dict(os.environ)
        )
        self._model_catalog_directory = model_catalog_directory or os.getcwd()
        self._auto_confirm_extensions = auto_confirm_extensions
        self._client_factory = client_factory or self._default_client_factory
        self._sessions: set[PiriSession] = set()
        self._memory_materializer_path = memory_materializer_path
        self._memory_bootstrap_timeout_seconds = memory_bootstrap_timeout_seconds
        self._memory_environment_validator = memory_environment_validator
        self._route_environment_factory = route_environment_factory
        self._session_directories: dict[str, Path] = {}

    async def start_or_resume(self, request: SessionRequest) -> PiriSession:
        _validate_full_access_request(request)
        _validate_cli_selection(request)
        command = [self._executable, "--mode", "rpc", "--approve"]
        environment = dict(self._process_environment)
        session_directory: Path | None = None
        if request.memory_environment is not None:
            if (
                self._memory_materializer_path is not None
                and self._memory_environment_validator is None
            ):
                raise ValueError("Piri memory materializer requires a route validator")
            if self._memory_environment_validator is not None:
                self._memory_environment_validator(request.memory_environment)
            environment.update(request.memory_environment)
            if self._memory_materializer_path is not None:
                session_directory = await self._prepare_memory_bootstrap(
                    command,
                    environment,
                )
        if request.session_id is not None:
            command.extend(("--session-id", request.session_id))
        if request.model is not None:
            command.extend(("--model", request.model))
        if request.effort is not None:
            command.extend(("--thinking", request.effort))

        config = PiriLaunchConfig(
            command=tuple(command),
            working_directory=request.working_directory,
            environment=environment,
            auto_confirm_extensions=self._auto_confirm_extensions,
        )
        client = self._client_factory(config)
        try:
            await client.start()
            state = await client.get_state()
        except asyncio.CancelledError:
            with suppress(Exception):
                await client.close()
            raise
        except Exception:
            with suppress(Exception):
                await client.close()
            raise RuntimeError("Piri runtime failed to start") from None

        session_id = state.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            await client.close()
            raise RuntimeError("Piri did not return a stable session id")
        if request.session_id is not None and session_id != request.session_id:
            await client.close()
            raise RuntimeError("Piri resumed a different session id")
        if session_directory is not None:
            self._session_directories[session_id] = session_directory

        session = PiriSession(
            session_id,
            client,
            on_close=self._sessions.discard,
        )
        self._sessions.add(session)
        return session

    async def _prepare_memory_bootstrap(
        self,
        command: list[str],
        environment: dict[str, str],
    ) -> Path:
        session_value = environment.get("PIRI_CODING_AGENT_SESSION_DIR")
        bootstrap_value = environment.get("CCC_PIRI_BOOTSTRAP_HOME")
        context_value = environment.get("CCC_PIRI_BOOTSTRAP_CONTEXT_FILE")
        if not session_value or not bootstrap_value or not context_value:
            raise ValueError("Piri memory route is incomplete")
        session_directory = Path(session_value)
        bootstrap_home = Path(bootstrap_value)
        context_file = Path(context_value)
        if context_file != bootstrap_home / "AGENTS.md":
            raise ValueError("Piri memory context path is invalid")
        ensure_private_directory(session_directory)
        ensure_private_directory(bootstrap_home)
        bootstrap_environment = dict(environment)
        bootstrap_environment["CODEX_HOME"] = str(bootstrap_home)
        bootstrap_environment["CODEX_SQLITE_HOME"] = str(bootstrap_home)
        bootstrap_environment["CCC_MEMORY_MATERIALIZER_PROVIDER"] = "piri"
        await _run_codex_memory_bootstrap(
            self._memory_materializer_path or "",
            timeout_seconds=self._memory_bootstrap_timeout_seconds,
            environment=bootstrap_environment,
        )
        self._validate_memory_context(context_file)
        command.extend(("--no-context-files", "--append-system-prompt", str(context_file)))
        return session_directory

    @staticmethod
    def _validate_memory_context(path: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError:
            raise RuntimeError("Piri memory bootstrap unavailable") from None
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or (hasattr(os, "geteuid") and metadata.st_uid != os.geteuid())
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_size > 1024 * 1024
            ):
                raise RuntimeError("Piri memory bootstrap unavailable")
        finally:
            os.close(descriptor)

    async def read_session_snapshot(
        self,
        session_id: str,
        *,
        bounds: TranscriptBounds,
        memory_audience: str | None = None,
        memory_scope: str | None = None,
    ) -> CodexTranscriptSnapshot:
        session_directory: Path | None = None
        if memory_audience is not None or memory_scope is not None:
            if (
                not isinstance(memory_audience, str)
                or not isinstance(memory_scope, str)
                or self._route_environment_factory is None
            ):
                raise ValueError("Piri snapshot memory route is invalid")
            route = self._route_environment_factory(memory_audience, memory_scope)
            value = route.get("PIRI_CODING_AGENT_SESSION_DIR")
            if not isinstance(value, str) or not value:
                raise ValueError("Piri snapshot memory route is incomplete")
            session_directory = Path(value)
        else:
            session_directory = self._session_directories.get(session_id)
        if session_directory is None:
            # Unscoped fallback: sessions launched without a memory route (or
            # before a bridge restart dropped the in-memory map) still store
            # transcripts under the default Piri sessions root. Locate the
            # holding directory read-only instead of failing the distill job.
            session_directory = await asyncio.to_thread(self._default_session_directory, session_id)
        if session_directory is None:
            raise ValueError("Piri snapshot session route is unavailable")
        return await asyncio.to_thread(
            read_piri_snapshot,
            session_directory,
            session_id,
            bounds=bounds,
        )

    def _default_session_directory(self, session_id: str) -> Path | None:
        """Resolve the default unscoped sessions root and scan it read-only."""

        environment = self._process_environment
        session_root = environment.get("PIRI_CODING_AGENT_SESSION_DIR")
        if isinstance(session_root, str) and session_root:
            root = Path(session_root)
        else:
            agent_dir = environment.get("PIRI_CODING_AGENT_DIR")
            if isinstance(agent_dir, str) and agent_dir:
                root = Path(agent_dir) / "sessions"
            else:
                home = environment.get("HOME")
                base = Path(home) if isinstance(home, str) and home else Path.home()
                root = base / ".piri" / "agent" / "sessions"
        try:
            return find_piri_session_directory(root, session_id)
        except OSError:
            return None

    async def list_models(self) -> Sequence[ModelInfo]:
        config = PiriLaunchConfig(
            command=(self._executable, "--mode", "rpc", "--approve", "--no-session"),
            working_directory=self._model_catalog_directory,
            environment=self._process_environment,
            auto_confirm_extensions=self._auto_confirm_extensions,
        )
        client = self._client_factory(config)
        try:
            await client.start()
            state = await client.get_state()
            current_model = state.get("model")
            current_id = _model_id(current_model) if isinstance(current_model, Mapping) else None
            models = await client.get_available_models()
            result: list[ModelInfo] = []
            for model in models:
                model_id = _model_id(model)
                name = model.get("name")
                reasoning = model.get("reasoning") is True
                if model_id is None:
                    continue
                result.append(
                    ModelInfo(
                        id=model_id,
                        display_name=name if isinstance(name, str) and name else model_id,
                        default_reasoning_effort="medium" if reasoning else None,
                        supported_reasoning_efforts=_supported_reasoning_levels(model),
                        is_default=model_id == current_id,
                    )
                )
            return tuple(result)
        finally:
            await client.close()

    @property
    def supports_session_browsing(self) -> bool:
        """Piri RPC 0.83 resumes exact ids but cannot list stored sessions."""

        return False

    async def close(self) -> None:
        sessions = tuple(self._sessions)
        self._sessions.clear()
        await asyncio.gather(*(session.close() for session in sessions), return_exceptions=True)

    @staticmethod
    def _default_client_factory(config: PiriLaunchConfig) -> PiriClient:
        return PiriRpcProcessClient(
            config.command,
            working_directory=config.working_directory,
            environment=config.environment,
            auto_confirm=config.auto_confirm_extensions,
        )


def _validate_full_access_request(request: SessionRequest) -> None:
    if request.approval_policy not in {None, "never"}:
        raise ValueError("PiriRuntime supports only approval_policy='never'")
    if request.approvals_reviewer is not None:
        raise ValueError("PiriRuntime has no approval reviewer")
    if request.sandbox_policy is None:
        return
    sandbox_type = request.sandbox_policy.get("type")
    if sandbox_type not in _FULL_ACCESS_SANDBOX_TYPES:
        raise ValueError("PiriRuntime supports only danger-full-access sandbox policy")


def _validate_cli_selection(request: SessionRequest) -> None:
    if (
        request.session_id is not None
        and _SESSION_ID_PATTERN.fullmatch(request.session_id) is None
    ):
        raise ValueError("Piri session id is invalid")
    if request.model is not None and _MODEL_ID_PATTERN.fullmatch(request.model) is None:
        raise ValueError("Piri model id is invalid")
    if request.effort is not None and request.effort not in _REASONING_LEVELS:
        raise ValueError("Piri thinking level is invalid")


def _tool_started(event: Mapping[str, Any]) -> ToolStartedEvent | None:
    tool_call_id = event.get("toolCallId")
    tool_name = event.get("toolName")
    arguments = event.get("args")
    if not isinstance(tool_call_id, str) or not tool_call_id:
        return None
    if not isinstance(tool_name, str) or not tool_name:
        return None
    if not isinstance(arguments, Mapping):
        arguments = {}
    return ToolStartedEvent(
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        arguments=cast(Mapping[str, Any], arguments),
    )


def _tool_completed(event: Mapping[str, Any]) -> ToolCompletedEvent | None:
    tool_call_id = event.get("toolCallId")
    tool_name = event.get("toolName")
    if not isinstance(tool_call_id, str) or not tool_call_id:
        return None
    if not isinstance(tool_name, str) or not tool_name:
        return None
    return ToolCompletedEvent(
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        result=cast(Any, event.get("result")),
        success=event.get("isError") is not True,
    )


@dataclass(frozen=True, slots=True)
class _MessageEndResult:
    stop_reason: str
    terminal_error: ErrorEvent | None
    completed_message: bool


@dataclass(slots=True)
class _TurnState:
    last_stop_reason: str = "end_turn"
    terminal_error: ErrorEvent | None = None
    assistant_message_open: bool = False


def _normalized_events(
    event: Mapping[str, Any],
    state: _TurnState,
) -> tuple[AgentEvent, ...]:
    event_type = event.get("type")
    if event_type == "message_update":
        translated, saw_text = _message_update_events(event)
        state.assistant_message_open = state.assistant_message_open or saw_text
        return translated
    if event_type == "tool_execution_start":
        started_tool = _tool_started(event)
        return (started_tool,) if started_tool is not None else ()
    if event_type == "tool_execution_end":
        completed_tool = _tool_completed(event)
        return (completed_tool,) if completed_tool is not None else ()
    if event_type != "message_end":
        return ()
    message_result = _message_end_result(
        event,
        assistant_message_open=state.assistant_message_open,
    )
    if message_result is None:
        return ()
    state.last_stop_reason = message_result.stop_reason
    state.terminal_error = message_result.terminal_error
    state.assistant_message_open = False
    return (MessageCompletedEvent(),) if message_result.completed_message else ()


def _terminal_events(
    state: _TurnState,
    *,
    session_id: str,
    interrupted: bool,
) -> tuple[AgentEvent, ...]:
    if state.terminal_error is not None:
        return (state.terminal_error,)
    if interrupted:
        return (
            ErrorEvent(
                code="interrupted",
                message="Piri turn was interrupted",
            ),
        )
    return (
        ResultEvent(
            {
                "provider": "piri",
                "session_id": session_id,
                "status": "completed",
            }
        ),
        CompletionEvent(state.last_stop_reason),
    )


def _message_update_events(
    event: Mapping[str, Any],
) -> tuple[tuple[AgentEvent, ...], bool]:
    update = event.get("assistantMessageEvent")
    if not isinstance(update, Mapping):
        return (), False
    update_type = update.get("type")
    delta = update.get("delta")
    if not isinstance(delta, str) or not delta:
        return (), False
    if update_type == "text_delta":
        return (TextDeltaEvent(delta),), True
    if update_type == "thinking_delta":
        return (ReasoningDeltaEvent(delta),), False
    return (), False


def _message_end_result(
    event: Mapping[str, Any],
    *,
    assistant_message_open: bool,
) -> _MessageEndResult | None:
    provider_message = event.get("message")
    if not isinstance(provider_message, Mapping) or provider_message.get("role") != "assistant":
        return None
    raw_stop_reason = provider_message.get("stopReason")
    stop_reason = (
        raw_stop_reason
        if isinstance(raw_stop_reason, str) and raw_stop_reason
        else "end_turn"
    )
    if stop_reason == "aborted":
        terminal_error = ErrorEvent(
            code="interrupted",
            message="Piri turn was interrupted",
        )
    elif stop_reason == "error":
        terminal_error = ErrorEvent(
            code="piri_turn_failed",
            message="Piri provider turn failed",
        )
    else:
        # A later successful assistant message means Piri's automatic retry recovered.
        terminal_error = None
    return _MessageEndResult(
        stop_reason=stop_reason,
        terminal_error=terminal_error,
        completed_message=assistant_message_open,
    )


def _model_id(model: Mapping[str, Any]) -> str | None:
    provider = model.get("provider")
    model_id = model.get("id")
    if not isinstance(provider, str) or not provider:
        return None
    if not isinstance(model_id, str) or not model_id:
        return None
    return f"{provider}/{model_id}"


def _supported_reasoning_levels(model: Mapping[str, Any]) -> tuple[str, ...]:
    if model.get("reasoning") is not True:
        return ()
    level_map = model.get("thinkingLevelMap")
    if not isinstance(level_map, Mapping):
        return _REASONING_LEVELS
    return tuple(
        level
        for level in _REASONING_LEVELS
        if level_map.get(level, "supported") is not None
    )
