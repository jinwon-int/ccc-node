"""Provider-neutral runtime adapter for the Codex app-server protocol."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import logging
import math
import os
from pathlib import Path
import re
import signal
import time
from typing import Literal, Protocol, cast

from .agent_runtime import (
    AgentEvent,
    AsyncCompletionCapability,
    ApprovalDecision,
    ApprovalHandler,
    ApprovalRequestEvent,
    CompletionEvent,
    ErrorEvent,
    JsonValue as AgentJsonValue,
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
from .async_completion_delivery import bounded_completion_text
from .codex_app_server import (
    CodexAppServerClient,
    CodexNotification,
    CodexServerRequest,
    CodexThread,
    CodexThreadListPage,
    JsonValue,
    ServerRequestHandler,
)
from .usage import (
    SNAPSHOT_TTL_SECONDS,
    UsageSnapshot,
    merge_usage,
    parse_codex_account_usage,
    parse_codex_rate_limits,
    parse_codex_thread_usage,
)
from .working_state_archive import (
    archive_working_state,
    select_working_state_environment,
)
from telegram_bot.memory.codex_rollout import (
    read_codex_rollout_candidates,
    validate_rollout_root,
)
from telegram_bot.memory.distill_types import (
    CodexTranscriptSnapshot,
    SnapshotUnavailableError,
    TranscriptBounds,
    TranscriptMessage,
)
from .turn_stall import find_rollout


logger = logging.getLogger(__name__)
_MEMORY_BOOTSTRAP_MAX_OUTPUT = 16384
MemoryBootstrap = Callable[[], Awaitable[None]]


async def _stop_bootstrap_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except OSError:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(process.wait(), timeout=0.25)
        return
    except TimeoutError:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(process.wait(), timeout=1.0)
    except TimeoutError:
        pass


async def _run_materializer_command(
    path: str,
    command: str,
    timeout_seconds: float,
    *,
    environment: Mapping[str, str] | None = None,
) -> bool:
    try:
        if environment is None:
            process = await asyncio.create_subprocess_exec(
                path,
                command,
                "--json",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
        else:
            process = await asyncio.create_subprocess_exec(
                path,
                command,
                "--json",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
                env=dict(environment),
            )
    except OSError:
        return False
    try:
        stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except asyncio.CancelledError:
        await _stop_bootstrap_process(process)
        raise
    except TimeoutError:
        await _stop_bootstrap_process(process)
        return False
    return process.returncode == 0 and len(stdout) <= _MEMORY_BOOTSTRAP_MAX_OUTPUT


async def _run_codex_memory_bootstrap(
    path: str,
    *,
    timeout_seconds: float,
    environment: Mapping[str, str] | None = None,
) -> None:
    if not path.strip() or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise RuntimeError("codex memory bootstrap unavailable")
    if await _run_materializer_command(
        path,
        "materialize",
        timeout_seconds,
        environment=environment,
    ):
        return
    if await _run_materializer_command(
        path,
        "status",
        timeout_seconds,
        environment=environment,
    ):
        logger.warning("Codex memory refresh failed; using the last valid snapshot")
        return
    raise RuntimeError("codex memory bootstrap unavailable")


class AppServerClient(Protocol):
    async def start(self) -> JsonValue: ...

    async def thread_start(self, *, cwd: str, model: str | None = None) -> JsonValue: ...

    async def thread_resume(
        self,
        thread_id: str,
        *,
        cwd: str | None = None,
        model: str | None = None,
    ) -> JsonValue: ...

    async def thread_rollback(self, thread_id: str, *, num_turns: int) -> JsonValue: ...

    async def turn_start(
        self,
        thread_id: str,
        input_items: Sequence[Mapping[str, JsonValue]],
        *,
        model: str | None = None,
        effort: str | None = None,
        approval_policy: str | None = None,
        approvals_reviewer: str | None = None,
        sandbox_policy: Mapping[str, JsonValue] | None = None,
    ) -> JsonValue: ...

    async def turn_interrupt(self, thread_id: str, turn_id: str) -> JsonValue: ...

    async def list_models(self, *, include_hidden: bool = False) -> JsonValue: ...

    async def account_rate_limits(self) -> JsonValue: ...

    async def account_usage(self) -> JsonValue: ...

    async def thread_list(
        self, *, limit: int = 20, cursor: str | None = None
    ) -> CodexThreadListPage: ...

    async def thread_read(
        self, thread_id: str, *, include_turns: bool = True
    ) -> CodexThread | None: ...

    async def next_notification(self) -> CodexNotification: ...

    async def close(self) -> None: ...


ClientFactory = Callable[[ServerRequestHandler], AppServerClient]
UsageRecorder = Callable[[str, UsageSnapshot | None, UsageSnapshot], object]
_CODEX_OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_RECENT_TERMINAL_TURN_LIMIT = 512
# Mid-turn usage coalescing horizon: bounds both the worst-case metering loss
# on a hard crash and how stale the durable meter can run behind a long turn.
_USAGE_COALESCE_MAX_AGE_SECONDS = 5.0
_ASYNC_DIAGNOSTIC_COUNTER_MAX = (1 << 31) - 1


@dataclass(frozen=True, slots=True)
class AsyncCompletionDiagnostics:
    """Body-free evidence for the intentionally degraded Codex boundary.

    Codex app-server currently has no documented notification that proves a
    completed turn is an owned detached/background task.  The runtime therefore
    keeps ignoring otherwise-valid ``turn/completed`` notifications that do not
    match its exact active turn.  These counters make that fail-closed behavior
    observable without retaining provider ids, items, output, or raw payloads.
    """

    state: Literal["degraded"]
    unowned_completed: int
    late_active_duplicates: int
    recent_terminal_turns: int


@dataclass(slots=True)
class _ActiveTurn:
    queue: asyncio.Queue[AgentEvent]
    approval_handler: ApprovalHandler
    turn_id: str | None = None
    turn_ready: asyncio.Event = field(default_factory=asyncio.Event)
    finished: bool = False
    pending_notifications: list[CodexNotification] = field(default_factory=list)
    # Whether assistant text has been emitted since the last message boundary.
    # Gates the paragraph separator so consecutive agentMessages are split (but a
    # separator never leads, and an empty message can't double it).
    emitted_text: bool = False


class CodexSession:
    """One provider-neutral session backed by a Codex thread."""

    def __init__(
        self,
        runtime: CodexRuntime,
        thread_id: str,
        model: str | None,
        effort: str | None,
        approval_policy: str | None,
        approvals_reviewer: str | None,
        sandbox_policy: Mapping[str, AgentJsonValue] | None,
        turn_lock: asyncio.Lock,
        working_state_environment: Mapping[str, str] | None = None,
    ) -> None:
        self._runtime = runtime
        self._thread_id = thread_id
        self._model = model
        self._effort = effort
        self._approval_policy = approval_policy
        self._approvals_reviewer = approvals_reviewer
        self._sandbox_policy = cast(Mapping[str, JsonValue] | None, sandbox_policy)
        self._turn_lock = turn_lock
        self._working_state_environment = select_working_state_environment(
            working_state_environment
        )
        self._closed = False

    @property
    def session_id(self) -> str:
        return self._thread_id

    def send_turn(
        self,
        message: str,
        *,
        approval_handler: ApprovalHandler = deny_approval,
    ) -> AsyncIterator[AgentEvent]:
        async def events() -> AsyncIterator[AgentEvent]:
            async with self._turn_lock:
                active = _ActiveTurn(asyncio.Queue(), approval_handler)
                self._runtime._active_turns[self._thread_id] = active
                try:
                    result = await self._runtime._client.turn_start(
                        self._thread_id,
                        [{"type": "text", "text": message}],
                        model=self._model,
                        effort=self._effort,
                        approval_policy=self._approval_policy,
                        approvals_reviewer=self._approvals_reviewer,
                        sandbox_policy=self._sandbox_policy,
                    )
                    returned_turn_id = self._runtime._turn_id(result)
                    if active.turn_id is not None and active.turn_id != returned_turn_id:
                        raise RuntimeError("Codex approval turn does not match turn/start response")
                    active.turn_id = returned_turn_id
                    runtime = self._runtime
                    runtime._started_turn_ids[returned_turn_id] = None
                    runtime._started_turn_ids = dict(
                        tuple(runtime._started_turn_ids.items())[-512:]
                    )
                    runtime._record_turn_attempt()
                    active.turn_ready.set()
                    self._runtime._flush_pending_notifications(active)
                    while True:
                        event = await active.queue.get()
                        yield event
                        if isinstance(event, (CompletionEvent, ErrorEvent)):
                            return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    yield ErrorEvent(
                        code="codex_runtime_error",
                        message=str(exc) or "Codex runtime request failed",
                    )
                finally:
                    active.finished = True
                    active.turn_ready.set()
                    # The turn boundary is the natural durability point for
                    # the usage coalesced across this turn's token updates.
                    self._runtime._flush_thread_usage(self._thread_id)
                    if self._runtime._active_turns.get(self._thread_id) is active:
                        self._runtime._active_turns.pop(self._thread_id, None)

        return events()

    async def interrupt(self) -> None:
        active = self._runtime._active_turns.get(self._thread_id)
        if active is None or active.turn_id is None or active.finished:
            return
        await self._runtime._client.turn_interrupt(self._thread_id, active.turn_id)

    async def close(self) -> None:
        """Archive conversation state without closing the shared app-server."""

        if self._closed:
            return
        self._closed = True
        environment = self._working_state_environment
        if environment is None:
            return
        try:
            await asyncio.to_thread(
                archive_working_state,
                "session_end",
                environment=environment,
                session_id=self._thread_id,
            )
        except Exception:
            # Provider/working-state bodies must never enter diagnostics, and
            # best-effort preservation must not block conversation cleanup.
            logger.warning("Codex working-state archive failed event=session_end")


class CodexRuntime:
    """Own a shared Codex app-server client and its notification dispatcher."""

    def __init__(
        self,
        *,
        cli_path: str = "codex",
        client_factory: ClientFactory | None = None,
        process_environment: Mapping[str, str] | None = None,
        working_state_environment: Mapping[str, str] | None = None,
        memory_materializer_path: str | None = None,
        memory_bootstrap_timeout_seconds: float = 14.0,
        memory_bootstrap: MemoryBootstrap | None = None,
    ) -> None:
        if not cli_path.strip():
            raise ValueError("Codex CLI path must not be empty")
        bound_environment: dict[str, str] | None = None
        if process_environment is not None:
            bound_environment = dict(os.environ)
            for name, value in process_environment.items():
                if not isinstance(name, str) or not name or "\x00" in name:
                    raise ValueError("Codex process environment name is invalid")
                if not isinstance(value, str) or "\x00" in value:
                    raise ValueError("Codex process environment value is invalid")
                bound_environment[name] = value
        if memory_bootstrap is not None and memory_materializer_path is not None:
            raise ValueError("configure either memory_bootstrap or memory_materializer_path")
        if memory_materializer_path is not None:
            path = memory_materializer_path.strip()
            if not path:
                raise ValueError("Codex memory materializer path must not be empty")
            if (
                not math.isfinite(memory_bootstrap_timeout_seconds)
                or memory_bootstrap_timeout_seconds <= 0
            ):
                raise ValueError("Codex memory bootstrap timeout must be positive")

            async def configured_bootstrap() -> None:
                await _run_codex_memory_bootstrap(
                    path,
                    timeout_seconds=memory_bootstrap_timeout_seconds,
                    environment=bound_environment,
                )

            memory_bootstrap = configured_bootstrap
        if client_factory is not None:
            self._client_factory = client_factory
        elif bound_environment is None:

            def default_client_factory(
                handler: ServerRequestHandler,
            ) -> AppServerClient:
                return CodexAppServerClient(
                    executable=cli_path,
                    server_request_handler=handler,
                )

            self._client_factory = default_client_factory
        else:

            def environment_client_factory(
                handler: ServerRequestHandler,
            ) -> AppServerClient:
                return CodexAppServerClient(
                    executable=cli_path,
                    process_environment=bound_environment,
                    server_request_handler=handler,
                )

            self._client_factory = environment_client_factory
        self._client = self._client_factory(self._handle_server_request)
        self._process_environment = bound_environment
        self._working_state_environment = select_working_state_environment(
            working_state_environment
            if working_state_environment is not None
            else bound_environment
        )
        self._memory_bootstrap = memory_bootstrap
        self._memory_bootstrap_lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._started = False
        self._dispatcher_task: asyncio.Task[None] | None = None
        self._active_turns: dict[str, _ActiveTurn] = {}
        self._thread_locks: dict[str, asyncio.Lock] = {}
        self._thread_usage: dict[str, UsageSnapshot] = {}
        # Coalesced mid-turn usage per thread: (burst-start previous snapshot,
        # latest snapshot, burst-start monotonic time). tokenUsage/updated
        # fires many times per streamed turn, and each recorder call is a
        # durable flock+reload+rewrite of the meter file; totals are
        # cumulative, so recording the burst once at the turn boundary (or
        # after the max-age below) telescopes to the identical delta.
        self._pending_thread_usage: dict[
            str, tuple[UsageSnapshot | None, UsageSnapshot, float]
        ] = {}
        # Threads created (not resumed) by this process: their cumulative
        # usage totals contain no prior-session history, so the first
        # observation is real new spend rather than a resume baseline.
        self._created_threads: dict[str, None] = {}
        # Turn ids this process started, so a resumed thread's first usage
        # notification can be attributed to our turn (metered via the
        # turn-scoped `last` block) instead of being discarded as history.
        self._started_turn_ids: dict[str, None] = {}
        self._account_rate_limits = UsageSnapshot(provider="codex")
        self._usage_recorder: UsageRecorder | None = None
        self._turn_attempt_recorder: Callable[[], object] | None = None
        # Bounded body-free tombstones distinguish a delayed duplicate of a
        # bridge-owned terminal turn from a different unowned completion.  The
        # ids never leave this in-memory map and are not exposed by diagnostics.
        self._recent_terminal_turn_ids: dict[str, None] = {}
        self._ignored_unowned_completions = 0
        self._ignored_late_active_duplicates = 0
        # Optional body-free observer for validated unowned completions
        # (#646 slice 1). The runtime never awaits or trusts the listener; a
        # raising listener is swallowed exactly like a failed meter write.
        self._unowned_completion_listener: Callable[[str, str], object] | None = None
        # Optional promotion listener for runtimes that declare durable
        # delivery (#646 slice 2). Receives the same fail-closed-validated
        # observation plus the bounded body text (or None); invoked only when
        # async_completion_capability() reports supports_durable_delivery.
        self._durable_completion_listener: (
            Callable[[str, str, str | None], object] | None
        ) = None
        self._closed = False

    @property
    def supports_async_completion_delivery(self) -> bool:
        """Whether detached Codex completion delivery has a safe provider seam."""

        return self.async_completion_capability().supports_durable_delivery

    @staticmethod
    def async_completion_capability() -> AsyncCompletionCapability:
        """Pin the official surfaces without claiming detached-turn ownership.

        App-server documents ``turn/completed`` and ``thread/read``, but its
        initialize response does not negotiate a protocol version or an event
        field tying an unowned turn to a bridge-started detached task.  Treat
        that unknown version/ownership combination as degraded even when both
        method names happen to work for ordinary interactive turns.
        """

        return AsyncCompletionCapability(
            provider="codex",
            state="degraded",
            protocol_version=None,
            notification_method="turn/completed",
            recovery_method="thread/read",
            ownership_scope="exact_active_turn",
            supports_durable_delivery=False,
            reason_code="detached_ownership_unavailable",
        )

    def async_completion_diagnostics(self) -> AsyncCompletionDiagnostics:
        """Return only bounded counters; never expose provider payload content."""

        return AsyncCompletionDiagnostics(
            state="degraded",
            unowned_completed=self._ignored_unowned_completions,
            late_active_duplicates=self._ignored_late_active_duplicates,
            recent_terminal_turns=len(self._recent_terminal_turn_ids),
        )

    def set_unowned_completion_listener(
        self, listener: Callable[[str, str], object] | None
    ) -> None:
        """Observe validated unowned completions without promoting them.

        The listener receives ``(thread_id, turn_id)`` only after every
        fail-closed shape check in ``_observe_unowned_completion`` passed, so
        it never sees malformed or provider-foreign input.  It must be
        synchronous and non-blocking; exceptions are swallowed because the
        degraded no-delivery boundary must never break the turn path.
        """

        self._unowned_completion_listener = listener

    def set_durable_completion_listener(
        self, listener: Callable[[str, str, str | None], object] | None
    ) -> None:
        """Promote validated unowned completions to durable delivery (#646).

        The listener receives ``(thread_id, turn_id, text)`` where ``text`` is
        the bounded agent-message body (``None`` when none is extractable).
        It is invoked only when :meth:`async_completion_capability` declares
        ``supports_durable_delivery`` — a degraded runtime never promotes and
        never hands out provider payload text.  The same synchronicity and
        exception-swallowing rules as the slice-1 observer apply.
        """

        self._durable_completion_listener = listener

    def set_turn_attempt_recorder(self, recorder: Callable[[], object]) -> None:
        """Observe each turn/start the provider accepted (the spend boundary).

        Invoked exactly once per successful ``turn/start`` response — before
        any event is consumed — so an attempt cancelled while waiting for its
        first event is still counted, while a ``turn/start`` that failed
        before reaching the provider charges nothing. Fail-open: recorder
        exceptions are logged and never break the turn.
        """

        self._turn_attempt_recorder = recorder

    def _record_turn_attempt(self) -> None:
        if self._turn_attempt_recorder is None:
            return
        try:
            self._turn_attempt_recorder()
        except Exception:
            logger.exception("Turn attempt recorder failed; turn continues")

    def set_usage_recorder(self, recorder: UsageRecorder) -> None:
        """Observe per-thread cumulative usage snapshots (previous, current).

        The recorder is fail-open: it is invoked from the notification
        dispatcher and any exception it raises is swallowed after logging so
        provider event routing can never be broken by metering.
        """

        self._usage_recorder = recorder

    async def _bootstrap_memory(self) -> None:
        if self._memory_bootstrap is None:
            return
        async with self._memory_bootstrap_lock:
            await self._memory_bootstrap()

    async def _ensure_started(self) -> None:
        async with self._start_lock:
            if self._closed:
                raise RuntimeError("Codex runtime is closed")
            if self._started:
                return
            await self._client.start()
            self._dispatcher_task = asyncio.create_task(self._dispatch_notifications())
            self._started = True

    async def start_or_resume(self, request: SessionRequest) -> CodexSession:
        await self._bootstrap_memory()
        await self._ensure_started()
        if request.session_id is None:
            result = await self._client.thread_start(
                cwd=request.working_directory,
                model=request.model,
            )
            thread_id = self._thread_id(result)
            self._created_threads[thread_id] = None
            self._created_threads = dict(tuple(self._created_threads.items())[-256:])
            turn_lock = self._thread_locks.setdefault(thread_id, asyncio.Lock())
        else:
            thread_id = request.session_id
            turn_lock = self._thread_locks.setdefault(thread_id, asyncio.Lock())
            async with turn_lock:
                result = await self._client.thread_resume(
                    thread_id,
                    cwd=request.working_directory,
                    model=request.model,
                )
                if self._thread_id(result) != thread_id:
                    raise RuntimeError("Codex resume returned a different thread")
                if self._has_orphaned_dynamic_tool_call(result):
                    logger.warning(
                        "Recovering Codex thread by rolling back its last incomplete "
                        "dynamic-tool turn"
                    )
                    try:
                        recovered = await self._client.thread_rollback(
                            thread_id,
                            num_turns=1,
                        )
                        if self._thread_id(recovered) != thread_id:
                            raise RuntimeError("Codex rollback returned a different thread")
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        raise RuntimeError(
                            "Codex orphaned tool-call recovery failed"
                        ) from None
        return CodexSession(
            self,
            thread_id,
            request.model,
            request.effort,
            request.approval_policy,
            request.approvals_reviewer,
            request.sandbox_policy,
            turn_lock,
            request.memory_environment or self._working_state_environment,
        )

    async def list_models(self) -> Sequence[ModelInfo]:
        await self._ensure_started()
        result = await self._client.list_models()
        if not isinstance(result, Mapping):
            return ()
        data = result.get("data")
        if not isinstance(data, (list, tuple)):
            return ()
        models: list[ModelInfo] = []
        for value in data:
            if not isinstance(value, Mapping):
                continue
            model_id = value.get("id")
            display_name = value.get("displayName", value.get("name"))
            if not isinstance(model_id, str) or not model_id:
                continue
            if not isinstance(display_name, str) or not display_name:
                continue
            raw_efforts = value.get("supportedReasoningEfforts")
            efforts: list[str] = []
            if isinstance(raw_efforts, (list, tuple)):
                for option in raw_efforts:
                    if not isinstance(option, Mapping):
                        continue
                    effort = option.get("reasoningEffort")
                    if isinstance(effort, str) and effort and effort not in efforts:
                        efforts.append(effort)
            default_effort = value.get("defaultReasoningEffort")
            if not isinstance(default_effort, str) or not default_effort:
                default_effort = None
            models.append(
                ModelInfo(
                    id=model_id,
                    display_name=display_name,
                    default_reasoning_effort=default_effort,
                    supported_reasoning_efforts=tuple(efforts),
                    is_default=value.get("isDefault") is True,
                )
            )
        return tuple(models)

    async def get_usage(self, thread_id: str | None) -> UsageSnapshot:
        """Read account usage and merge exact-thread notifications, without a turn."""

        await self._ensure_started()

        async def safely(call, parser) -> UsageSnapshot:
            try:
                value = await asyncio.wait_for(call(), timeout=5.0)
            except Exception:
                return UsageSnapshot(provider="codex")
            return parser(value)

        rate_limits, account = await asyncio.gather(
            safely(self._client.account_rate_limits, parse_codex_rate_limits),
            safely(self._client.account_usage, parse_codex_account_usage),
        )
        if rate_limits.observed_at is not None:
            self._account_rate_limits = rate_limits
        now = time.time()
        cached_rate_limits = self._account_rate_limits
        if (
            cached_rate_limits.observed_at is None
            or now - cached_rate_limits.observed_at > SNAPSHOT_TTL_SECONDS
        ):
            cached_rate_limits = UsageSnapshot(provider="codex")
        result = merge_usage(cached_rate_limits, account)
        if thread_id:
            thread = self._thread_usage.get(thread_id)
            if (
                thread is not None
                and thread.observed_at is not None
                and now - thread.observed_at <= SNAPSHOT_TTL_SECONDS
            ):
                result = merge_usage(result, thread)
        return result

    @property
    def supports_session_browsing(self) -> bool:
        return True

    async def list_sessions(
        self,
        *,
        limit: int = 10,
        max_pages: int = 5,
    ) -> Sequence[SessionSummary]:
        """Return a bounded list of app-server threads."""

        if limit <= 0 or max_pages <= 0:
            return ()
        await self._ensure_started()
        bounded_limit = min(limit, 100)
        bounded_pages = min(max_pages, 5)
        cursor: str | None = None
        seen_cursors: set[str] = set()
        summaries: list[SessionSummary] = []
        for _ in range(bounded_pages):
            page = await self._client.thread_list(
                limit=min(20, bounded_limit - len(summaries)),
                cursor=cursor,
            )
            for value in page.data:
                if len(summaries) >= bounded_limit:
                    break
                summaries.append(
                    SessionSummary(
                        id=value.id,
                        title=value.title,
                        preview=value.preview,
                        updated_at=value.updated_at,
                        cwd=value.cwd,
                        model=value.model,
                    )
                )
            if len(summaries) >= bounded_limit or page.next_cursor is None:
                break
            if page.next_cursor in seen_cursors:
                break
            seen_cursors.add(page.next_cursor)
            cursor = page.next_cursor
        return tuple(summaries)

    async def read_session(self, session_id: str, *, limit: int = 5) -> SessionHistory:
        """Return only bounded user and assistant text from ``thread/read``."""

        if not session_id:
            raise ValueError("session id must not be empty")
        if limit <= 0:
            return SessionHistory(session_id, ())
        await self._ensure_started()
        thread = await self._client.thread_read(session_id, include_turns=True)
        if thread is None:
            return SessionHistory(session_id, ())
        messages: list[SessionHistoryMessage] = []
        for turn in thread.turns[-100:]:
            timestamp = self._history_timestamp(turn.get("createdAt"))
            items = turn.get("items")
            if not isinstance(items, (list, tuple)):
                continue
            for item in items[:200]:
                if not isinstance(item, Mapping):
                    continue
                item_type = item.get("type")
                role: Literal["user", "assistant"]
                text: object
                if item_type == "userMessage":
                    text = self._user_message_text(item.get("content"))
                    role = "user"
                elif item_type == "agentMessage":
                    text = item.get("text")
                    role = "assistant"
                else:
                    continue
                if not isinstance(text, str):
                    continue
                bounded_text = text[:2000].strip()
                if not bounded_text:
                    continue
                item_timestamp = self._history_timestamp(item.get("timestamp")) or timestamp
                messages.append(
                    SessionHistoryMessage(role, bounded_text, item_timestamp)
                )
        return SessionHistory(session_id, tuple(messages[-min(limit, 50):]))

    async def read_session_snapshot(
        self,
        session_id: str,
        *,
        bounds: TranscriptBounds | None = None,
        now: datetime | None = None,
        memory_audience: str | None = None,
        memory_scope: str | None = None,
    ) -> CodexTranscriptSnapshot:
        """Return a strict, read-only and byte-bounded Codex transcript snapshot."""

        if not session_id:
            raise ValueError("session id must not be empty")
        if memory_audience is not None or memory_scope is not None:
            raise ValueError("direct Codex runtime cannot route an audience snapshot")
        limits = bounds or TranscriptBounds()
        captured = now or datetime.now(timezone.utc)
        if captured.tzinfo is None:
            captured = captured.replace(tzinfo=timezone.utc)
        captured = captured.astimezone(timezone.utc)
        await self._ensure_started()
        thread = await self._client.thread_read(session_id, include_turns=True)
        thread_hash = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        if thread is None:
            # The app-server is authoritative only while it still holds the
            # thread. Distill snapshots are taken by a worker that runs after
            # the interactive path enqueued the job (#475's trigger contract
            # never awaits a provider call), so by then the read returns
            # nothing. Fall back to the rollout file, which outlives the
            # process. Fail closed if that is unavailable too: returning an
            # empty snapshot made the pipeline report success while discarding
            # the session (snapshot_done -> no transcript -> honcho: []).
            disk = await asyncio.to_thread(
                self._rollout_snapshot, session_id, limits, captured, thread_hash
            )
            if disk is not None:
                return disk
            raise SnapshotUnavailableError(
                f"Codex thread is not readable for snapshot: {thread_hash}"
            )

        newest_messages, last_turn_id, structural_truncation = (
            self._snapshot_candidates(thread, limits, captured)
        )
        messages, byte_count, byte_truncation = self._bound_snapshot_messages(
            newest_messages, limits
        )
        return CodexTranscriptSnapshot(
            thread_hash=thread_hash,
            last_turn_id=last_turn_id,
            messages=messages,
            byte_count=byte_count,
            truncated=structural_truncation or byte_truncation,
            captured_at=self._format_snapshot_time(captured),
        )

    def _rollout_snapshot(
        self,
        session_id: str,
        limits: TranscriptBounds,
        captured: datetime,
        thread_hash: str,
    ) -> CodexTranscriptSnapshot | None:
        """Rebuild the snapshot from this runtime's rollout files, or None.

        The search is confined to the CODEX_HOME this runtime was constructed
        with. CodexRuntimePool gives every audience its own process and its own
        CODEX_HOME (memory_audience.codex_environment), so staying inside it is
        what keeps the fallback from reading across audience partitions — the
        isolation the pool's own thread-ownership check provides on the RPC
        path. Never widen this to a default or global sessions root.
        """

        environment: Mapping[str, str] = self._process_environment or os.environ
        configured = (environment.get("CODEX_HOME") or "").strip()
        codex_home = Path(configured) if configured else Path.home() / ".codex"
        try:
            validate_rollout_root(codex_home / "sessions")
        except (OSError, ValueError) as error:
            logger.warning(
                "codex rollout fallback root unusable thread=%s reason=%s",
                thread_hash,
                type(error).__name__,
            )
            return None
        path = find_rollout([codex_home], session_id)
        if path is None:
            return None
        try:
            newest_messages, last_turn_id, truncated = read_codex_rollout_candidates(
                path, session_id, limits=limits, captured=captured
            )
        except (OSError, ValueError) as error:
            # A rollout that fails its identity or safety contract is not a
            # substitute for the thread. Returning None keeps the caller on the
            # fail-closed path instead of attributing another session's text.
            logger.warning(
                "codex rollout fallback rejected thread=%s reason=%s",
                thread_hash,
                type(error).__name__,
            )
            return None
        if not newest_messages:
            return None
        messages, byte_count, byte_truncation = self._bound_snapshot_messages(
            newest_messages, limits
        )
        if not messages:
            return None
        logger.info(
            "codex snapshot served from rollout file thread=%s messages=%d",
            thread_hash,
            len(messages),
        )
        return CodexTranscriptSnapshot(
            thread_hash=thread_hash,
            last_turn_id=last_turn_id,
            messages=messages,
            byte_count=byte_count,
            truncated=truncated or byte_truncation,
            captured_at=self._format_snapshot_time(captured),
        )

    @classmethod
    def _snapshot_candidates(
        cls,
        thread: CodexThread,
        limits: TranscriptBounds,
        captured: datetime,
    ) -> tuple[list[TranscriptMessage], str | None, bool]:
        turns = tuple(thread.turns)
        selected_turns = turns[-limits.max_turns :]
        truncated = len(turns) > limits.max_turns
        last_turn_id: str | None = None
        if selected_turns:
            raw_last_turn_id = selected_turns[-1].get("id")
            if isinstance(raw_last_turn_id, str) and raw_last_turn_id:
                last_turn_id = raw_last_turn_id

        newest_messages: list[TranscriptMessage] = []
        items_seen = 0
        for turn in reversed(selected_turns):
            turn_timestamp = cls._history_timestamp(turn.get("createdAt"))
            items = turn.get("items")
            if not isinstance(items, (list, tuple)):
                continue
            for item in reversed(items):
                if items_seen >= limits.max_items:
                    return newest_messages, last_turn_id, True
                items_seen += 1
                message, excluded_by_age = cls._snapshot_item(
                    item, turn_timestamp, captured, limits.max_age_seconds
                )
                truncated = truncated or excluded_by_age
                if message is None:
                    continue
                newest_messages.append(message)
                if len(newest_messages) >= limits.max_messages:
                    return newest_messages, last_turn_id, True
        return newest_messages, last_turn_id, truncated

    @classmethod
    def _snapshot_item(
        cls,
        item: object,
        turn_timestamp: str | None,
        captured: datetime,
        max_age_seconds: int,
    ) -> tuple[TranscriptMessage | None, bool]:
        if not isinstance(item, Mapping):
            return None, False
        item_type = item.get("type")
        role: Literal["user", "assistant"]
        text: object
        if item_type == "userMessage":
            text = cls._user_message_text(item.get("content"))
            role = "user"
        elif item_type == "agentMessage":
            text = item.get("text")
            role = "assistant"
        else:
            return None, False
        if not isinstance(text, str) or not (text := text.strip()):
            return None, False
        timestamp = cls._history_timestamp(item.get("timestamp")) or turn_timestamp
        parsed_timestamp = cls._parse_snapshot_time(timestamp)
        if parsed_timestamp is None:
            return None, True
        if (captured - parsed_timestamp).total_seconds() > max_age_seconds:
            return None, True
        return TranscriptMessage(role, text, timestamp), False

    @classmethod
    def _bound_snapshot_messages(
        cls,
        newest_messages: list[TranscriptMessage],
        limits: TranscriptBounds,
    ) -> tuple[tuple[TranscriptMessage, ...], int, bool]:
        bounded_newest: list[TranscriptMessage] = []
        remaining_bytes = limits.max_bytes
        truncated = False
        for message in newest_messages:
            if remaining_bytes <= 0:
                truncated = True
                break
            allowed_bytes = min(remaining_bytes, limits.max_message_bytes)
            bounded_text, was_truncated = cls._truncate_utf8(
                message.text, allowed_bytes
            )
            truncated = truncated or was_truncated
            if not bounded_text:
                continue
            bounded_newest.append(
                TranscriptMessage(message.role, bounded_text, message.timestamp)
            )
            remaining_bytes -= len(bounded_text.encode("utf-8"))
        messages = tuple(reversed(bounded_newest))
        byte_count = sum(len(message.text.encode("utf-8")) for message in messages)
        return messages, byte_count, truncated

    @staticmethod
    def _truncate_utf8(value: str, maximum_bytes: int) -> tuple[str, bool]:
        payload = value.encode("utf-8")
        if len(payload) <= maximum_bytes:
            return value, False
        return payload[:maximum_bytes].decode("utf-8", errors="ignore").strip(), True

    @staticmethod
    def _parse_snapshot_time(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _format_snapshot_time(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _user_message_text(content: JsonValue) -> str | None:
        if isinstance(content, str):
            return content[:2000]
        if not isinstance(content, (list, tuple)):
            return None
        parts: list[str] = []
        for block in content[:50]:
            if not isinstance(block, Mapping):
                continue
            if block.get("type") not in {"text", "input_text"}:
                continue
            text = block.get("text")
            if isinstance(text, str):
                bounded_text = text[:2000].strip()
                if bounded_text:
                    parts.append(bounded_text)
        return "\n".join(parts) or None

    @staticmethod
    def _history_timestamp(value: object) -> str | None:
        return value if isinstance(value, str) and value else None

    async def close(self) -> None:
        async with self._start_lock:
            if self._closed:
                return
            self._closed = True
            self._flush_thread_usage()
            self._fail_active_turns("codex_runtime_closed", "Codex runtime closed")
            if self._dispatcher_task is not None:
                self._dispatcher_task.cancel()
                await asyncio.gather(self._dispatcher_task, return_exceptions=True)
                self._dispatcher_task = None
            self._started = False
            await self._client.close()

    async def recycle(self) -> bool:
        """Replace an idle app-server connection and its MCP subprocess tree.

        Codex threads are durable outside this process. The bridge discards its
        lightweight ``CodexSession`` wrappers before calling this method, then
        reconstructs them with ``thread/resume`` on the next message.
        """

        async with self._start_lock:
            if self._closed or self._active_turns:
                return False
            if not self._started:
                return False

            dispatcher = self._dispatcher_task
            self._dispatcher_task = None
            self._started = False
            if dispatcher is not None:
                dispatcher.cancel()
                await asyncio.gather(dispatcher, return_exceptions=True)
            self._thread_locks.clear()
            self._thread_usage.clear()
            self._created_threads.clear()
            self._started_turn_ids.clear()
            self._account_rate_limits = UsageSnapshot(provider="codex")
            close_task = asyncio.create_task(self._client.close())
            try:
                await asyncio.shield(close_task)
            except asyncio.CancelledError:
                # Rebuild only after the old process tree reached its bounded
                # close boundary. Leaving a closed client installed would make
                # the next thread/resume permanently fail after guard timeout
                # or process shutdown cancellation.
                await asyncio.gather(close_task, return_exceptions=True)
                self._client = self._client_factory(self._handle_server_request)
                raise
            except Exception:
                logger.exception("Codex app-server close failed during idle recycle")

            self._client = self._client_factory(self._handle_server_request)
            logger.info("Recycled idle Codex app-server connection")
            return True

    async def _dispatch_notifications(self) -> None:
        try:
            while True:
                notification = await self._client.next_notification()
                try:
                    self._route_notification(notification)
                except (TypeError, ValueError):
                    continue
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fail_active_turns("codex_connection_failed", str(exc))

    def _route_notification(self, notification: CodexNotification) -> None:
        params = notification.params
        if notification.method == "account/rateLimits/updated":
            update = parse_codex_rate_limits(params)
            self._account_rate_limits = merge_usage(self._account_rate_limits, update)
            return
        thread_id = params.get("threadId")
        if not isinstance(thread_id, str):
            return
        if notification.method == "thread/tokenUsage/updated":
            self._record_thread_usage(thread_id, params)
            return
        active = self._routable_active_turn(notification, thread_id)
        if active is None:
            return
        if active.turn_id is None:
            active.pending_notifications.append(notification)
            return
        turn_id = self._notification_turn_id(params)
        if not self._notification_matches_active_turn(active, notification, turn_id):
            return

        event: AgentEvent | None = None
        if notification.method == "item/agentMessage/delta":
            delta = params.get("delta")
            if isinstance(delta, str) and delta:
                event = TextDeltaEvent(delta)
                active.emitted_text = True
        elif notification.method in {
            "item/reasoning/textDelta",
            "item/reasoning/summaryTextDelta",
        }:
            delta = params.get("delta")
            if isinstance(delta, str) and delta:
                event = ReasoningDeltaEvent(delta)
        elif notification.method in {"item/started", "item/completed"}:
            event = self._tool_event(notification.method, params)
            if (
                event is None
                and notification.method == "item/completed"
                and active.emitted_text
                and self._is_agent_message_item(params)
            ):
                # Keep lifecycle separate from content. The consumer can now
                # deliver an interim message before a tool while preserving the
                # terminal message for the normal final-response path.
                event = MessageCompletedEvent()
                active.emitted_text = False
        elif notification.method == "turn/completed":
            self._complete_turn(active, params)
            return
        if event is not None:
            active.queue.put_nowait(event)

    def _notification_matches_active_turn(
        self,
        active: _ActiveTurn,
        notification: CodexNotification,
        observed_turn_id: str | None,
    ) -> bool:
        """Accept this turn's event or reconcile a steered submission id."""
        if (
            active.turn_id is None
            or observed_turn_id is None
            or active.turn_id == observed_turn_id
        ):
            return True
        return self._reconcile_steered_turn_id(
            active,
            notification,
            observed_turn_id,
        )

    def _reconcile_steered_turn_id(
        self,
        active: _ActiveTurn,
        notification: CodexNotification,
        observed_turn_id: str,
    ) -> bool:
        """Adopt the canonical active turn after app-server steers our input.

        Codex app-server currently returns a provisional submission UUID from
        ``turn/start`` when the thread already has an active turn, even though
        Core steers the submitted user message into that existing turn.  All
        lifecycle notifications then carry the existing turn id.  Treat only
        the provider's ``item/started`` acknowledgement for the submitted user
        message as strong enough evidence to reconcile the ids; arbitrary late
        deltas or terminal notifications from another turn remain ignored.

        This is a client-side compatibility guard for openai/codex#36866.  It
        can stay harmless after the provider fix because matching ids never
        enter this branch.
        """

        if notification.method != "item/started":
            return False
        item = notification.params.get("item")
        if not isinstance(item, Mapping) or item.get("type") != "userMessage":
            return False
        if _CODEX_OPAQUE_ID_RE.fullmatch(observed_turn_id) is None:
            return False

        provisional_turn_id = active.turn_id
        if provisional_turn_id is None or provisional_turn_id == observed_turn_id:
            return False
        active.turn_id = observed_turn_id
        self._started_turn_ids.pop(provisional_turn_id, None)
        self._started_turn_ids[observed_turn_id] = None
        self._started_turn_ids = dict(tuple(self._started_turn_ids.items())[-512:])
        logger.warning(
            "Reconciled Codex turn/start submission id to the active turn "
            "after a steered user-message acknowledgement"
        )
        return True

    def _routable_active_turn(
        self,
        notification: CodexNotification,
        thread_id: str,
    ) -> _ActiveTurn | None:
        active = self._active_turns.get(thread_id)
        if active is None:
            self._observe_unowned_completion(notification, thread_id)
            return None
        if active.finished:
            # A duplicate of the exact terminal notification may arrive before
            # send_turn's finally removes the active entry. A different turn
            # while that finished owner is still present is an active mismatch,
            # not an async-completion observation.
            if self._notification_turn_id(notification.params) == active.turn_id:
                self._observe_unowned_completion(notification, thread_id)
            return None
        return active

    def _observe_unowned_completion(
        self,
        notification: CodexNotification,
        thread_id: str,
    ) -> None:
        """Count, but never promote, one otherwise-valid unowned completion.

        A known thread plus a successful ``turn/completed`` shape does not prove
        detached-task ownership.  It may be another client, a cleanup race, or
        a delayed duplicate.  Keep the current no-delivery behavior and retain
        only aggregate evidence for future provider-capability work (#646).
        """

        if notification.method != "turn/completed":
            return
        if thread_id not in self._thread_locks:
            return
        turn = notification.params.get("turn")
        if not isinstance(turn, Mapping):
            return
        turn_id = turn.get("id")
        if not isinstance(turn_id, str) or _CODEX_OPAQUE_ID_RE.fullmatch(turn_id) is None:
            return
        if turn.get("status") != "completed":
            return
        items = turn.get("items")
        if not isinstance(items, list):
            return
        items_view = turn.get("itemsView", "full")
        if items_view != "full":
            return
        if turn_id in self._recent_terminal_turn_ids:
            self._ignored_late_active_duplicates = min(
                self._ignored_late_active_duplicates + 1,
                _ASYNC_DIAGNOSTIC_COUNTER_MAX,
            )
        else:
            self._ignored_unowned_completions = min(
                self._ignored_unowned_completions + 1,
                _ASYNC_DIAGNOSTIC_COUNTER_MAX,
            )
            listener = self._unowned_completion_listener
            if listener is not None:
                try:
                    listener(thread_id, turn_id)
                except Exception:
                    logger.warning(
                        "Unowned completion listener failed; degraded no-delivery "
                        "behavior is unchanged",
                        exc_info=True,
                    )
            if self.async_completion_capability().supports_durable_delivery:
                durable_listener = self._durable_completion_listener
                if durable_listener is not None:
                    text = bounded_completion_text(turn.get("items"))
                    try:
                        durable_listener(thread_id, turn_id, text)
                    except Exception:
                        logger.warning(
                            "Durable completion listener failed; promotion "
                            "skipped for this observation",
                            exc_info=True,
                        )

    def _remember_terminal_turn(self, turn_id: str) -> None:
        if _CODEX_OPAQUE_ID_RE.fullmatch(turn_id) is None:
            return
        self._recent_terminal_turn_ids[turn_id] = None
        self._recent_terminal_turn_ids = dict(
            tuple(self._recent_terminal_turn_ids.items())[-_RECENT_TERMINAL_TURN_LIMIT:]
        )

    def _record_thread_usage(self, thread_id: str, params: Mapping[str, JsonValue]) -> None:
        previous = self._thread_usage.get(thread_id)
        snapshot = parse_codex_thread_usage(params)
        if previous is None:
            if thread_id in self._created_threads:
                # A thread this process created has no prior-session history
                # in its cumulative totals: a zero baseline records the first
                # turn's spend instead of discarding it as resume history.
                previous = UsageSnapshot(
                    provider="codex", input_tokens=0, output_tokens=0
                )
            elif self._is_our_turn_notification(thread_id, params):
                # A resumed thread's first observation during OUR turn mixes
                # prior-session history with the new turn. The turn-scoped
                # `last` block sizes the new spend, so the implied pre-turn
                # baseline (total - last) excludes history without dropping
                # the first paid turn.
                previous = self._implied_pre_turn_baseline(params, snapshot)
        self._thread_usage[thread_id] = snapshot
        self._thread_usage = dict(tuple(self._thread_usage.items())[-128:])
        if self._usage_recorder is None:
            return
        pending = self._pending_thread_usage.get(thread_id)
        if pending is not None:
            # Keep the burst's original baseline; only the endpoint advances.
            previous = pending[0]
            burst_started = pending[2]
        else:
            burst_started = time.monotonic()
        if (
            self._is_our_turn_notification(thread_id, params)
            and time.monotonic() - burst_started
            < _USAGE_COALESCE_MAX_AGE_SECONDS
        ):
            self._pending_thread_usage[thread_id] = (
                previous,
                snapshot,
                burst_started,
            )
            return
        self._pending_thread_usage.pop(thread_id, None)
        try:
            self._usage_recorder(thread_id, previous, snapshot)
        except Exception:
            logger.exception("Codex usage recorder failed; dispatch continues")

    def _flush_thread_usage(self, thread_id: str | None = None) -> None:
        """Durably record coalesced usage for one thread (or every thread)."""
        if thread_id is None:
            items = tuple(self._pending_thread_usage.items())
            self._pending_thread_usage.clear()
        else:
            pending = self._pending_thread_usage.pop(thread_id, None)
            items = ((thread_id, pending),) if pending is not None else ()
        if self._usage_recorder is None:
            return
        for pending_thread_id, (previous, snapshot, _started) in items:
            try:
                self._usage_recorder(pending_thread_id, previous, snapshot)
            except Exception:
                logger.exception(
                    "Codex usage recorder failed; dispatch continues"
                )

    def _is_our_turn_notification(
        self, thread_id: str, params: Mapping[str, JsonValue]
    ) -> bool:
        turn_id = self._notification_turn_id(params)
        if turn_id is not None and turn_id in self._started_turn_ids:
            return True
        active = self._active_turns.get(thread_id)
        return active is not None and not active.finished

    @staticmethod
    def _implied_pre_turn_baseline(
        params: Mapping[str, JsonValue], snapshot: UsageSnapshot
    ) -> UsageSnapshot | None:
        """Derive the pre-turn cumulative baseline from the `last` block.

        ``total - last`` stays constant across mid-turn updates, so metering
        the delta against it counts exactly the current turn. Without a
        parseable ``last`` the observation stays a plain baseline (history is
        never charged to the budget).
        """

        token_usage = params.get("tokenUsage", params)
        if not isinstance(token_usage, Mapping):
            return None
        last = token_usage.get("last")
        if not isinstance(last, Mapping):
            return None

        def _count(value: object) -> int | None:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return None
            return value

        last_input = _count(last.get("inputTokens"))
        last_output = _count(last.get("outputTokens"))
        if last_input is None and last_output is None:
            # Older shape: only the turn total is exposed. Split it against
            # the cumulative totals — take what input can absorb, then the
            # remainder from output — so output-heavy first resumed turns
            # keep their full turn total instead of losing the output share
            # to the zero clamp below.
            last_total = _count(last.get("totalTokens"))
            if last_total is None:
                return None
            last_input = min(snapshot.input_tokens or 0, last_total)
            last_output = last_total - last_input
        return UsageSnapshot(
            provider="codex",
            input_tokens=max(0, (snapshot.input_tokens or 0) - (last_input or 0)),
            output_tokens=max(0, (snapshot.output_tokens or 0) - (last_output or 0)),
        )

    def _flush_pending_notifications(self, active: _ActiveTurn) -> None:
        pending = tuple(active.pending_notifications)
        active.pending_notifications.clear()
        for notification in pending:
            self._route_notification(notification)

    @staticmethod
    def _is_agent_message_item(params: Mapping[str, JsonValue]) -> bool:
        item = params.get("item")
        return isinstance(item, Mapping) and item.get("type") == "agentMessage"

    @staticmethod
    def _tool_event(method: str, params: Mapping[str, JsonValue]) -> AgentEvent | None:
        item = params.get("item")
        if not isinstance(item, Mapping):
            return None
        item_id = item.get("id")
        tool_name = item.get("type")
        if not isinstance(item_id, str) or not item_id:
            return None
        if not isinstance(tool_name, str) or not tool_name:
            return None
        if tool_name in {
            "agentMessage",
            "userMessage",
            "reasoning",
            "plan",
            "enteredReviewMode",
            "exitedReviewMode",
        }:
            return None
        snapshot = cast(Mapping[str, AgentJsonValue], item)
        if method == "item/started":
            return ToolStartedEvent(item_id, tool_name, snapshot)
        status = item.get("status")
        exit_code = item.get("exitCode")
        success = status in {"completed", "success"} and exit_code in {None, 0}
        return ToolCompletedEvent(item_id, tool_name, snapshot, success)

    def _complete_turn(self, active: _ActiveTurn, params: Mapping[str, JsonValue]) -> None:
        turn = params.get("turn")
        if not isinstance(turn, Mapping):
            return
        turn_id = turn.get("id")
        status = turn.get("status")
        if status in {"completed", "success"}:
            active.queue.put_nowait(ResultEvent(cast(AgentJsonValue, turn)))
            active.queue.put_nowait(CompletionEvent("end_turn"))
            if isinstance(turn_id, str):
                self._remember_terminal_turn(turn_id)
        elif status in {"interrupted", "cancelled"}:
            active.queue.put_nowait(ErrorEvent("interrupted", "Codex turn was interrupted"))
        else:
            error = turn.get("error")
            message = (str(error) if error is not None else "") or "Codex turn failed"
            active.queue.put_nowait(ErrorEvent("codex_turn_failed", message))
        active.finished = True

    def _fail_active_turns(self, code: str, message: str) -> None:
        normalized_message = message or "Codex connection failed"
        for active in tuple(self._active_turns.values()):
            if not active.finished:
                active.queue.put_nowait(ErrorEvent(code, normalized_message))

    async def _handle_server_request(
        self,
        request: CodexServerRequest,
    ) -> Mapping[str, JsonValue]:
        approval_methods = {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
            "item/permissions/requestApproval",
        }
        if request.method not in approval_methods:
            return self._unsupported_server_request()

        thread_id = request.params.get("threadId")
        turn_id = request.params.get("turnId")
        active: _ActiveTurn | None = None
        if isinstance(thread_id, str):
            active = self._active_turns.get(thread_id)
        if active is None or active.finished or not isinstance(turn_id, str):
            return self._approval_response(request.method, ApprovalDecision.DENY, request.params)
        if active.turn_id is None:
            # A server request task can be scheduled before turn/start returns.
            # Wait for the exact returned turn ID before exposing approval UI or
            # sending an allow decision. A missing response remains fail-closed.
            try:
                await asyncio.wait_for(active.turn_ready.wait(), timeout=5.0)
            except TimeoutError:
                return self._approval_response(
                    request.method, ApprovalDecision.DENY, request.params
                )
        if active.finished or active.turn_id != turn_id:
            return self._approval_response(request.method, ApprovalDecision.DENY, request.params)

        approval = ApprovalRequestEvent(
            request_id=str(request.id),
            action=request.method,
            arguments=cast(Mapping[str, AgentJsonValue], request.params),
            description=f"Codex requests approval for {request.method.rsplit('/', 1)[0]}",
        )
        active.queue.put_nowait(approval)
        try:
            decision = await active.approval_handler(approval)
        except asyncio.CancelledError:
            raise
        except Exception:
            decision = ApprovalDecision.DENY
        if active.finished or self._active_turns.get(cast(str, thread_id)) is not active:
            decision = ApprovalDecision.DENY
        return self._approval_response(request.method, decision, request.params)

    @staticmethod
    def _approval_response(
        method: str,
        decision: ApprovalDecision,
        params: Mapping[str, JsonValue],
    ) -> Mapping[str, JsonValue]:
        if method == "item/permissions/requestApproval":
            permissions = params.get("permissions")
            allowed = permissions if decision is ApprovalDecision.ALLOW else {}
            if not isinstance(allowed, Mapping):
                allowed = {}
            return {"result": {"permissions": dict(allowed), "scope": "turn"}}
        provider_decision = "accept" if decision is ApprovalDecision.ALLOW else "decline"
        return {"result": {"decision": provider_decision}}

    @staticmethod
    def _unsupported_server_request() -> Mapping[str, JsonValue]:
        return {
            "error": {
                "code": -32601,
                "message": "Client does not support server request",
            }
        }

    @staticmethod
    def _thread_id(result: JsonValue) -> str:
        if not isinstance(result, Mapping):
            raise RuntimeError("Codex thread response is malformed")
        thread = result.get("thread")
        if not isinstance(thread, Mapping):
            raise RuntimeError("Codex thread response is missing thread")
        thread_id = thread.get("id")
        if not isinstance(thread_id, str) or not thread_id:
            raise RuntimeError("Codex thread response has invalid thread id")
        return thread_id

    @staticmethod
    def _has_orphaned_dynamic_tool_call(result: JsonValue) -> bool:
        """Detect a persisted client-tool request that can no longer finish.

        Codex exposes dynamic tool calls through the normalized thread view. If
        the app-server is idle but the last incomplete turn still contains an
        in-progress client tool with no output, resuming it would replay a
        response item that can never be matched. Only this narrow terminal
        shape is safe to prune; active and completed turns are preserved.
        """

        if not isinstance(result, Mapping):
            return False
        thread = result.get("thread")
        if not isinstance(thread, Mapping):
            return False
        thread_status = thread.get("status")
        if isinstance(thread_status, Mapping):
            thread_status = thread_status.get("type")
        if thread_status != "idle":
            return False
        turns = thread.get("turns")
        if not isinstance(turns, (list, tuple)) or not turns:
            return False
        last_turn = turns[-1]
        if not isinstance(last_turn, Mapping):
            return False
        if last_turn.get("status") not in {"inProgress", "interrupted", "failed"}:
            return False
        items = last_turn.get("items")
        if not isinstance(items, (list, tuple)):
            return False
        orphan_types = {"dynamicToolCall", "customToolCall", "custom_tool_call"}
        return any(
            isinstance(item, Mapping)
            and item.get("type") in orphan_types
            and item.get("status") == "inProgress"
            and item.get("contentItems") is None
            and item.get("success") is not True
            for item in items
        )

    @staticmethod
    def _turn_id(result: JsonValue) -> str:
        if not isinstance(result, Mapping):
            raise RuntimeError("Codex turn response is malformed")
        turn = result.get("turn")
        if not isinstance(turn, Mapping):
            raise RuntimeError("Codex turn response is missing turn")
        turn_id = turn.get("id")
        if not isinstance(turn_id, str) or not turn_id:
            raise RuntimeError("Codex turn response has invalid turn id")
        return turn_id

    @staticmethod
    def _notification_turn_id(params: Mapping[str, JsonValue]) -> str | None:
        turn_id = params.get("turnId")
        if isinstance(turn_id, str):
            return turn_id
        turn = params.get("turn")
        if isinstance(turn, Mapping):
            nested_id = turn.get("id")
            if isinstance(nested_id, str):
                return nested_id
        return None
