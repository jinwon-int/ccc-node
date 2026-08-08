"""Provider-neutral runtime adapter for the Claude Agent SDK (#584 P3-1).

``ClaudeRuntime`` implements the ``AgentRuntime`` protocol from
``core.agent_runtime`` on top of ``ClaudeSDKClient`` and has been the only
Claude path since the #584 slice C-2 cutover removed the legacy direct SDK
stream path.  The SDK-frame -> event translation carries the legacy reader
loop's semantics (text deltas, message boundaries, tool lifecycle, terminal
results) re-expressed as the normalized ``AgentEvent`` stream that the
runtime conformance suite pins.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
import logging
from pathlib import Path
import re
from typing import Any, Literal, Protocol, cast
import uuid

from claude_agent_sdk import (
    TERMINAL_TASK_STATUSES as SDK_TERMINAL_TASK_STATUSES,
    AssistantMessage,
    CanUseTool,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    EffortLevel,
    Message,
    PermissionMode,
    PermissionResult,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    ServerToolResultBlock,
    ServerToolUseBlock,
    StreamEvent,
    SystemMessage,
    TaskNotificationMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
    TextBlock,
    ThinkingBlock,
    ToolPermissionContext,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk.types import SandboxSettings

from telegram_bot.runtime_config_check import (
    DEFAULT_CLAUDE_MAX_BUFFER_SIZE,
    MAX_CLAUDE_MAX_BUFFER_SIZE,
    MIN_CLAUDE_MAX_BUFFER_SIZE,
)
from telegram_bot.utils.memory_policy import MEMORY_MODE_AUDIENCE_SCOPED, MEMORY_MODE_OFF
from telegram_bot.memory.claude_snapshot import read_claude_snapshot
from telegram_bot.memory.distill_types import (
    CodexTranscriptSnapshot,
    TranscriptBounds,
    validate_memory_route,
)

from .agent_runtime import (
    AgentEvent,
    ApprovalDecision,
    ApprovalHandler,
    ApprovalRequestEvent,
    CompletionEvent,
    DelegatedTaskLifecycleEvent,
    ErrorEvent,
    JsonValue,
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
from .curated_memory import build_curated_memory_settings
from .memory_audience import audience_from_claude_environment
from .project_chat_history import _first_text_block, iter_transcript_messages
from .sdk_text import _extract_stream_text_delta
from .tool_policy import (
    BASH_DISABLED,
    EXECUTION_OWNER_OPERATOR,
    EXECUTION_STRICT_PROJECT,
    claude_unrestricted_enabled,
    effective_bash_policy,
    resolve_bash_policy,
    resolve_execution_profile,
    running_as_root,
    sdk_permission_options,
    strict_bash_sandbox_settings,
)
from .web_mcp import build_curated_web_mcp

logger = logging.getLogger(__name__)

INTERRUPTED_ERROR_CODE = "interrupted"

_SNAKE_CASE_CODE = re.compile(r"^[a-z][a-z0-9_]*$")
# Session ids become transcript filenames; reject anything that could escape
# the transcripts directory (separators, a leading dot, empty).
_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_RETRYABLE_HTTP_STATUSES = frozenset({408, 429, 500, 502, 503, 504, 529})
_PERMISSION_MODES = frozenset(
    {"default", "acceptEdits", "plan", "bypassPermissions", "dontAsk", "auto"}
)
_EFFORT_LEVELS = frozenset({"low", "medium", "high", "xhigh", "max"})
_PREVIEW_SCAN_LIMIT = 50
_STDERR_TAIL_LINES = 20
_STDERR_LINE_CHARS = 400


def _classify_cli_stderr(lines: Sequence[str]) -> str | None:
    """Body-free error class for CLI stderr — never leak the payload.

    Mirrors ``external_wait_monitor._classify_gh_error``: a stall report needs
    the SHAPE of the failure, not its text. Provider stderr can echo prompts,
    filesystem paths, or a credential the CLI was handed, and a log line is the
    one place none of that may land.

    ``None`` means the process said nothing at all — itself the most telling
    answer when a turn produced no first event.
    """
    if not lines:
        return None
    text = " ".join(lines)[-_STDERR_LINE_CHARS:].casefold()
    if "rate limit" in text or "ratelimit" in text or "too many requests" in text:
        return "rate-limit"
    if (
        "not logged in" in text
        or "unauthorized" in text
        or "authentication" in text
        or "invalid api key" in text
    ):
        return "auth"
    if "certificate" in text or "ssl" in text or "tls handshake" in text:
        return "tls"
    if (
        "econnreset" in text
        or "econnrefused" in text
        or "enotfound" in text
        or "etimedout" in text
        or "socket hang up" in text
    ):
        return "network"
    if "timed out" in text or "timeout" in text:
        return "timeout"
    if "out of memory" in text or "enomem" in text or "heap" in text:
        return "oom"
    return "other"
_BACKGROUND_TASK_STARTED = re.compile(
    r"\bbackground\s+(?:task\s+)?with\s+id\s*:\s*([A-Za-z0-9][A-Za-z0-9_.-]{0,255})",
    re.IGNORECASE,
)
_SAFE_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,255}$")
_BACKGROUND_TASK_FINISHED = re.compile(
    r"<task-notification>.*?<task-id>([^<]{1,256})</task-id>.*?"
    r"<status>(completed|failed|stopped|killed|canceled|cancelled|timed_out|timeout)"
    r"</status>.*?"
    r"</task-notification>",
    re.IGNORECASE | re.DOTALL,
)
# Transcript XML predates the SDK's typed task frames and retains broader legacy
# terminal spellings (``canceled``, ``timed_out``, and aliases). Keep that
# compatibility vocabulary in the regex above; it is intentionally independent
# of the SDK-owned typed-frame statuses and may evolve separately.
#
# Only bounded delegated task types distinguish a result that ends one model
# turn from a result that ends the whole run. Shells, monitors, teammates, and
# remote agents may be intentionally long-lived and must not keep an interactive
# request open forever.
_RESULT_DEFERRING_TASK_TYPES = frozenset({"local_agent", "local_workflow"})
_NO_ACTIVE_APPROVAL_ROUTE = (
    "No active turn accepts approval requests; start a new user turn and retry"
)


_APPROVAL_PATH_KEYS = ("path", "file_path", "filePath", "paths", "target", "targets")


def _approval_target_kind(tool_input: object) -> str:
    """Body-free shape hint for an approval request (#889 observability).

    Returns only a kind label (``path``/``command``/empty) — never the value —
    so the log can say *what category* of target was asked about without
    exposing raw arguments, env, or file contents.
    """

    if not isinstance(tool_input, dict):
        return ""
    if any(isinstance(tool_input.get(k), str) and tool_input.get(k) for k in _APPROVAL_PATH_KEYS):
        return "path"
    if isinstance(tool_input.get("command"), str) and tool_input.get("command"):
        return "command"
    return ""


def _normalize_task_id(value: object) -> str | None:
    """Return one bounded opaque lifecycle key, never a task body."""

    if not isinstance(value, str):
        return None
    candidate = value.strip()
    return candidate if _SAFE_TASK_ID.fullmatch(candidate) else None

# The Claude CLI resolves these aliases itself; the bridge's /model surface is
# the same static curated set (model_discovery stays a curated list until the
# SDK exposes provider-side enumeration).
CURATED_CLAUDE_MODELS: tuple[ModelInfo, ...] = (
    ModelInfo(id="fable", display_name="Claude Fable", is_default=True),
    ModelInfo(id="sonnet", display_name="Claude Sonnet"),
    ModelInfo(id="opus", display_name="Claude Opus"),
    ModelInfo(id="haiku", display_name="Claude Haiku"),
)


class SdkClient(Protocol):
    """The subset of ``ClaudeSDKClient`` the runtime adapter depends on."""

    async def connect(self) -> None: ...

    async def query(self, prompt: str) -> None: ...

    def receive_messages(self) -> AsyncIterator[Message]: ...

    async def interrupt(self) -> None: ...

    async def disconnect(self) -> None: ...


SdkClientFactory = Callable[[ClaudeAgentOptions], SdkClient]

# Between-turns delivery seam: async (text, session_id) -> None. Delivers
# assistant output produced outside any ``send_turn`` (for example the CLI
# autonomously continuing after a harness background-task notification).
UnsolicitedHandler = Callable[[str, "str | None"], Awaitable[None]]

# Optional observation-only seam (#584 C-1 follow-up): a synchronous callback
# invoked with every raw SDK frame the session reads — turn-bearing and
# between-turns flows alike — so the bridge can observe the same
# ResultMessage usage/cost payloads and RateLimitEvent windows the direct
# reader loop feeds into its /usage recorders. Fire-and-forget and
# exception-isolated: a broken observer never affects turn processing, and
# runtimes without the seam (Codex) keep their current behavior.
SdkFrameObserver = Callable[[Message], None]


def _default_sdk_client_factory(options: ClaudeAgentOptions) -> SdkClient:
    return ClaudeSDKClient(options=options)


def _resolve_max_buffer_size(settings: Any) -> int:
    """Bytes for ``ClaudeAgentOptions.max_buffer_size``, never ``None``.

    Leaving the option unset hands the SDK its 1 MiB
    ``_DEFAULT_MAX_BUFFER_SIZE``, and one NDJSON line above that raises
    ``SDKJSONDecodeError`` inside the message reader — an unrecoverable
    whole-turn failure (measured 2026-08-03: a 1,056,854-byte line from a
    single screenshot whose base64 the CLI ships in two fields at once). So
    every construction path resolves to a real number here, including bare
    ``ClaudeRuntime()`` without bound settings (unit tests, the conformance
    harness), which would otherwise be the one route back to 1 MiB.

    Out-of-range or non-integer settings degrade to the default rather than
    failing session start: a mistyped buffer bound must not take the bridge
    down, and ``Config`` already validates the operator-facing value.
    """

    raw = getattr(settings, "claude_max_buffer_size", None)
    if isinstance(raw, bool) or not isinstance(raw, int):
        return DEFAULT_CLAUDE_MAX_BUFFER_SIZE
    if raw < MIN_CLAUDE_MAX_BUFFER_SIZE or raw > MAX_CLAUDE_MAX_BUFFER_SIZE:
        return DEFAULT_CLAUDE_MAX_BUFFER_SIZE
    return raw


@dataclass(slots=True)
class _ActiveTurn:
    queue: asyncio.Queue[AgentEvent]
    approval_handler: ApprovalHandler
    generation: int
    finished: bool = False
    interrupt_requested: bool = False
    # Whether assistant text has been emitted since the last message boundary,
    # so a MessageCompletedEvent never leads and empty messages emit none.
    emitted_text: bool = False
    # Whether the current SDK message already streamed via StreamEvent deltas;
    # gates the whole-block fallback so text is never emitted twice.
    streamed_current_message: bool = False
    # tool_call_id -> tool_name for started-but-not-completed tools, so the
    # completion event can carry the same name the start event declared.
    open_tools: dict[str, str] = field(default_factory=dict)
    # The Claude SDK can emit an intermediate ResultMessage while delegated
    # local work is still running.  Keep the exact turn/generation alive until
    # those tasks settle and their continuation emits the run-ending result.
    result_deferring_tasks: dict[str, float] = field(default_factory=dict)
    # Terminal lifecycle frames can overtake a buffered start.  This ledger is
    # scoped to one turn, so retaining every validated id until completion is
    # both finite and necessary to prevent a late start from re-opening it.
    result_deferring_terminal_tasks: set[str] = field(default_factory=set)
    completion_deferral_observed: bool = False


class ClaudeSession:
    """One provider-neutral session backed by a dedicated ``ClaudeSDKClient``."""

    def __init__(self, runtime: ClaudeRuntime, requested_session_id: str | None) -> None:
        self._runtime = runtime
        self._session_id: str | None = requested_session_id
        self._session_ready = asyncio.Event()
        self._client: SdkClient | None = None
        # Bounded: a looping CLI must not grow the session. Only the tail is
        # wanted anyway — the last thing the process said before going quiet.
        self._stderr_tail: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)
        self._reader_task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._turn_lock: asyncio.Lock | None = None
        self._active_turn: _ActiveTurn | None = None
        self._turn_generation = 0
        self._approval_counter = 0
        self._closed = False
        # Between-turns ("unsolicited") frame state (inherited from the
        # retired direct path's stream-state machinery):
        #   * handler — optional delivery route; absent = frames are dropped
        #     exactly as before this seam existed.
        #   * inflight — once a turn-bearing frame arrives without an active
        #     ``send_turn``, the autonomous turn keeps ownership of every
        #     frame through its terminal ResultMessage, even when a new user
        #     turn is submitted in between.
        #   * texts — assistant text buffered until that terminal result so
        #     one autonomous turn delivers as one message.
        #   * discard — a ``send_turn`` abandoned mid-turn (stall release,
        #     timeout, cancellation) may leak its late frames onto the
        #     between-turns listener; swallow them through the next terminal
        #     ResultMessage so an already-owned answer cannot deliver twice
        #     (the adapter counterpart of ``stall_swallow_result``).
        self._unsolicited_handler: UnsolicitedHandler | None = None
        self._unsolicited_inflight = False
        self._unsolicited_texts: list[str] = []
        self._unsolicited_discard = False
        # Claude Code reports run-in-background Bash ownership in the tool
        # result and later closes it with a terminal <task-notification>.  Keep
        # Typed SDK lifecycle frames and the authoritative shell roster carry
        # only validated opaque ids and monotonic start times; no command or
        # output body enters health.json.
        self._background_tasks: dict[str, float] = {}
        # Terminal frames and level-triggered rosters may overtake a buffered
        # start.  Remember finished ids for this provider session so an
        # arbitrary replay window cannot resurrect completed provider work.
        # The session lifecycle, plus the validated 256-byte id bound, owns the
        # storage boundary; correctness must not depend on an eviction count.
        self._background_task_terminal_ids: set[str] = set()
        self._sdk_frame_observer: SdkFrameObserver | None = None

    # -- lifecycle ---------------------------------------------------------

    async def _start(self, client: SdkClient, *, timeout_seconds: float) -> None:
        self._client = client
        try:
            await client.connect()
            self._reader_task = asyncio.create_task(self._read_frames(client))
            if self._session_id is None:
                # A new session's stable id is the SDK session id, announced by
                # the first system frame the CLI emits at startup.
                try:
                    await asyncio.wait_for(self._session_ready.wait(), timeout_seconds)
                except TimeoutError:
                    raise RuntimeError(
                        "Claude session id was not announced before the timeout"
                    ) from None
                if self._session_id is None:
                    raise RuntimeError("Claude session ended before announcing a session id")
            else:
                # Resume preserves the requested id as the stable neutral id.
                self._session_ready.set()
            self._turn_lock = self._runtime._session_lock(self._session_id)
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        close_task = self._begin_close()
        if close_task is not None:
            await asyncio.shield(close_task)

    def _begin_close(self) -> asyncio.Task[None] | None:
        """Synchronously seal the session and start its single cleanup task."""

        if self._close_task is None:
            if self._closed:
                return None
            self._closed = True
            self._fail_active_turn("claude_runtime_closed", "Claude runtime closed")
            reader_task = self._reader_task
            if reader_task is not None:
                reader_task.cancel()
                self._reader_task = None
            client = self._client
            if reader_task is None and client is None:
                return None
            # Keep cleanup in a separate single-flight task. Project-chat puts
            # stalled-turn abort behind asyncio.wait_for(); cancelling that
            # caller must not cancel the SDK's bounded TERM/KILL escalation.
            # A later close() joins the same task instead of trusting the
            # already-set _closed flag and abandoning a half-closed client.
            self._close_task = asyncio.create_task(
                self._finish_close(reader_task, client)
            )
        return self._close_task

    async def _finish_close(
        self,
        reader_task: asyncio.Task[None] | None,
        client: SdkClient | None,
    ) -> None:
        if reader_task is not None:
            await asyncio.gather(reader_task, return_exceptions=True)
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                logger.exception("Claude SDK client disconnect failed during close")
            finally:
                if self._client is client:
                    self._client = None
                self._runtime._forget_session(self)

    # -- transport diagnostics ---------------------------------------------

    def _record_stderr(self, line: object) -> None:
        """Sink for the CLI's stderr.

        Registering this is what makes the stderr exist at all: the SDK
        transport pipes stderr only when a callback is set
        (``stderr_dest = PIPE if self._options.stderr is not None else None``),
        so without it the kernel discards whatever the process said on its way
        out — the exact evidence an admission timeout needs (#846).

        Runs on the SDK's reader task, so it must never disturb the turn. The
        SDK already swallows callback exceptions; this does not rely on that.
        """
        try:
            text = str(line).strip()
        except Exception:  # pragma: no cover - defensive
            return
        if text:
            self._stderr_tail.append(text[:_STDERR_LINE_CHARS])

    def transport_diagnostics(self) -> dict[str, object]:
        """Why the runtime went quiet, in body-free form.

        Read this BEFORE the session is dropped: closing the client clears the
        SDK transport, and the process exit code goes with it.

        The exit code comes through private SDK attributes because the public
        surface exposes it only by raising ``ProcessError`` on a call we are not
        making here. Every hop is guarded — a diagnostic that raises while
        reporting a failure is worse than no diagnostic.
        """
        lines = tuple(self._stderr_tail)
        exit_code: object = None
        try:
            transport = getattr(self._client, "_transport", None)
            exit_code = getattr(getattr(transport, "_process", None), "returncode", None)
            if exit_code is None:
                exit_error = getattr(transport, "_exit_error", None)
                exit_code = getattr(exit_error, "exit_code", None)
        except Exception:  # pragma: no cover - defensive
            exit_code = None
        return {
            "exit_code": exit_code,
            "stderr_class": _classify_cli_stderr(lines),
            "stderr_lines": len(lines),
        }

    # -- AgentSession protocol ---------------------------------------------

    @property
    def session_id(self) -> str:
        if self._session_id is None:
            raise RuntimeError("Claude session is not started")
        return self._session_id

    def set_unsolicited_handler(self, handler: UnsolicitedHandler) -> None:
        """Register the between-turns delivery route (optional seam).

        Mirrors the style of the optional runtime seams project_chat probes
        via ``getattr`` (``set_usage_recorder`` / ``set_turn_attempt_recorder``
        on CodexRuntime): runtimes/sessions without the method keep their
        current behavior. The handler is fail-open — exceptions are logged and
        never break the reader task. Re-registration replaces the route.
        """

        self._unsolicited_handler = handler

    def set_sdk_frame_observer(self, observer: SdkFrameObserver) -> None:
        """Register the raw-SDK-frame observation route (optional seam).

        Same optional-seam style as ``set_unsolicited_handler``: callers
        probe it via ``getattr`` and sessions without it keep their current
        behavior. The observer runs synchronously for every frame the reader
        routes — turn and between-turns flows alike, including frames the
        discard machinery swallows — strictly for observation (the /usage
        usage-snapshot and rate-limit recorders). It is fail-open: exceptions
        are logged and never reach turn processing. Re-registration replaces
        the route.
        """

        self._sdk_frame_observer = observer

    def background_workload_snapshot(self, now: float) -> tuple[int, float]:
        """Return body-free tracked Claude background-task workload."""

        if not self._background_tasks:
            return 0, 0.0
        oldest_started = min(self._background_tasks.values())
        return len(self._background_tasks), max(0.0, float(now) - oldest_started)

    @staticmethod
    def _content_texts(value: Any) -> list[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, Mapping):
            texts: list[str] = []
            for item in value.values():
                texts.extend(ClaudeSession._content_texts(item))
            return texts
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            texts = []
            for item in value:
                texts.extend(ClaudeSession._content_texts(item))
            return texts
        return []

    def _observe_background_task_result(self, result: JsonValue) -> None:
        for text in self._content_texts(result):
            match = _BACKGROUND_TASK_STARTED.search(text)
            if match is not None:
                self._track_background_task_start(match.group(1))

    def _track_background_task_start(self, value: object) -> None:
        task_id = _normalize_task_id(value)
        if task_id is None or task_id in self._background_task_terminal_ids:
            return
        self._background_tasks.setdefault(task_id, asyncio.get_running_loop().time())

    def _finish_background_task(self, value: object) -> None:
        task_id = _normalize_task_id(value)
        if task_id is None:
            return
        self._background_tasks.pop(task_id, None)
        if task_id in self._background_task_terminal_ids:
            return
        self._background_task_terminal_ids.add(task_id)

    @staticmethod
    def _task_update_status(message: TaskUpdatedMessage) -> object:
        if message.status is not None:
            return message.status
        if isinstance(message.patch, Mapping):
            return message.patch.get("status")
        return None

    def _observe_background_task_system_message(self, message: SystemMessage) -> None:
        if message.subtype == "background_tasks_changed":
            self._reconcile_background_task_roster(message.data)
            return
        # Compatibility fallback for SDK parsers/stubs that preserve the raw
        # SystemMessage instead of constructing a typed subclass.
        data = message.data
        if message.subtype == "task_started":
            if data.get("task_type") == "local_bash":
                self._track_background_task_start(data.get("task_id"))
            return
        if message.subtype == "task_notification":
            self._finish_background_task(data.get("task_id"))
            return
        if message.subtype == "task_updated":
            patch = data.get("patch")
            status = patch.get("status") if isinstance(patch, Mapping) else None
            if status in SDK_TERMINAL_TASK_STATUSES:
                self._finish_background_task(data.get("task_id"))

    def _observe_background_task_notifications(self, message: Message) -> None:
        if isinstance(message, TaskStartedMessage):
            if message.task_type == "local_bash":
                self._track_background_task_start(message.task_id)
            return
        if isinstance(message, TaskNotificationMessage):
            # The SDK's TaskNotificationStatus Literal is terminal-only.
            self._finish_background_task(message.task_id)
            return
        if isinstance(message, TaskUpdatedMessage):
            if self._task_update_status(message) in SDK_TERMINAL_TASK_STATUSES:
                self._finish_background_task(message.task_id)
            return
        if isinstance(message, SystemMessage):
            self._observe_background_task_system_message(message)
            return
        if not isinstance(message, UserMessage):
            return
        for text in self._content_texts(message.content):
            for match in _BACKGROUND_TASK_FINISHED.finditer(text):
                self._finish_background_task(match.group(1))

    def _reconcile_background_task_roster(self, data: Mapping[str, Any]) -> None:
        """Replace the live shell roster, preserving known start timestamps."""

        tasks = data.get("tasks")
        if not isinstance(tasks, list):
            # A roster is a full replacement set. Fail closed if the payload is
            # not one, rather than erasing genuine tracked work.
            return
        local_bash_ids: set[str] = set()
        for task in tasks:
            if not isinstance(task, Mapping):
                return
            task_id = _normalize_task_id(task.get("task_id"))
            task_type = task.get("task_type")
            if (
                task_id is None
                or not isinstance(task_type, str)
                or not task_type
            ):
                return
            if task_type == "local_bash":
                local_bash_ids.add(task_id)
        # A full roster removes every formerly tracked id not present. Treat
        # that removal as terminal evidence so a delayed start edge cannot
        # re-register work the authoritative level has already dropped.
        for task_id in self._background_tasks.keys() - local_bash_ids:
            self._finish_background_task(task_id)
        local_bash_ids.difference_update(self._background_task_terminal_ids)
        now = asyncio.get_running_loop().time()
        self._background_tasks = {
            task_id: self._background_tasks.get(task_id, now)
            for task_id in local_bash_ids
        }

    def _observe_sdk_frame(self, message: Message) -> None:
        observer = self._sdk_frame_observer
        if observer is None:
            return
        try:
            observer(message)
        except Exception:
            # Observation-only seam: a broken observer must never affect the
            # frame routing that serves turns and unsolicited delivery.
            logger.exception("Claude SDK frame observer failed; frame routing continues")

    def send_turn(
        self,
        message: str,
        *,
        approval_handler: ApprovalHandler = deny_approval,
    ) -> AsyncIterator[AgentEvent]:
        async def events() -> AsyncIterator[AgentEvent]:
            client = self._client
            lock = self._turn_lock
            if client is None or lock is None:
                raise RuntimeError("Claude session is not started")
            async with lock:
                # A resumed waiter can capture its client before blocking on a
                # shared lock. Re-check after admission so _begin_close() can
                # seal it synchronously before the old owner releases (#625).
                if self._closed:
                    raise RuntimeError("Claude session is closed")
                self._runtime._turn_owners[self.session_id] = self
                self._turn_generation += 1
                active = _ActiveTurn(
                    asyncio.Queue(),
                    approval_handler,
                    self._turn_generation,
                )
                self._active_turn = active
                queried = False
                try:
                    await client.query(message)
                    queried = True
                    while True:
                        event = await active.queue.get()
                        yield event
                        if isinstance(event, (CompletionEvent, ErrorEvent)):
                            return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    yield ErrorEvent(
                        code="claude_runtime_error",
                        message=str(exc) or "Claude runtime request failed",
                    )
                finally:
                    if queried and not active.finished:
                        # Abandoned before its terminal frame (stall release,
                        # timeout, cancellation) while the provider turn may
                        # still be running: its late frames must be swallowed
                        # by the between-turns listener, not re-delivered as
                        # an unsolicited message.
                        self._unsolicited_discard = True
                    active.finished = True
                    if self._active_turn is active:
                        self._active_turn = None
                    if self._runtime._turn_owners.get(self.session_id) is self:
                        self._runtime._turn_owners.pop(self.session_id, None)

        return events()

    async def interrupt(self) -> None:
        owner = self._runtime._turn_owners.get(self.session_id)
        if owner is not None and owner is not self:
            await owner.interrupt()
            return
        active = self._active_turn
        client = self._client
        if active is None or active.finished or client is None:
            return
        active.interrupt_requested = True
        await client.interrupt()

    async def abort_stalled_turn(self) -> None:
        """Close the real lock owner and rotate its poisoned admission lock.

        A second session resumed with the same id shares ``_turn_lock``. If
        the first session lost its terminal frame, interrupting the waiter
        alone cannot release that lock and every recreated session would join
        the same dead queue. Closing the recorded owner terminates its reader;
        rotating only that session id lets the next clean session proceed
        while the abandoned generator unwinds on the old lock (#625).
        """

        session_id = self.session_id
        owner = self._runtime._turn_owners.get(session_id) or self
        owner_lock = owner._turn_lock
        # Preserve #625's waiter-before-owner safety without making the
        # waiter's full graceful disconnect a prerequisite for owner cleanup.
        # _begin_close() has no await: the waiter is sealed before the owner
        # can release the old lock, and send_turn re-checks that seal after
        # admission. The SDK cleanup tasks may then progress concurrently.
        close_tasks: list[asyncio.Task[None]] = []
        waiter_close = self._begin_close()
        if waiter_close is not None:
            close_tasks.append(waiter_close)
        if owner is not self:
            owner_close = owner._begin_close()
            if owner_close is not None:
                close_tasks.append(owner_close)
        try:
            await asyncio.gather(*(asyncio.shield(task) for task in close_tasks))
        finally:
            # The project-chat abort guard is deliberately shorter than the
            # SDK's worst-case graceful-close window. Rotate ownership even if
            # that guard expires; each close's shielded cleanup task keeps
            # running and remains joinable by a later close().
            if self._runtime._session_locks.get(session_id) is owner_lock:
                self._runtime._session_locks[session_id] = asyncio.Lock()
            if self._runtime._turn_owners.get(session_id) is owner:
                self._runtime._turn_owners.pop(session_id, None)

    # -- SDK frame translation ---------------------------------------------

    async def _read_frames(self, client: SdkClient) -> None:
        stream_failure: str | None = None
        try:
            async for message in client.receive_messages():
                try:
                    await self._route_message(message)
                except (TypeError, ValueError):
                    # One malformed frame must not take the connection down.
                    continue
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - defensive transport guard
            stream_failure = str(exc) or "Claude connection failed"
        finally:
            if not self._closed:
                self._fail_active_turn(
                    "claude_connection_failed",
                    stream_failure or "Claude connection closed",
                )
            # A closed stream can never announce an id; unblock _start.
            self._session_ready.set()

    async def _route_message(self, message: Message) -> None:
        self._observe_sdk_frame(message)
        self._observe_session_id(message)
        self._observe_background_task_notifications(message)
        active = self._active_turn
        if active is not None and not active.finished:
            delegated_event = self._observe_result_deferring_task(active, message)
            if delegated_event is not None:
                active.queue.put_nowait(delegated_event)
        if self._unsolicited_inflight or active is None or active.finished:
            # Same ownership rule as the direct reader loop
            # (``unsolicited_inflight or not state.pending``): a turn-bearing
            # frame that arrived without an active ``send_turn`` keeps every
            # frame through its terminal ResultMessage — a user turn submitted
            # in between must not steal the autonomous turn's result.
            await self._handle_unsolicited_frame(message)
            return
        if isinstance(message, StreamEvent):
            self._route_stream_event(active, message)
        elif isinstance(message, AssistantMessage):
            if message.parent_tool_use_id is None:
                self._route_assistant_message(active, message)
        elif isinstance(message, UserMessage):
            if message.parent_tool_use_id is None:
                self._route_tool_results(active, message)
        elif isinstance(message, ResultMessage):
            if active.result_deferring_tasks:
                # Body-free observability: task count is enough to explain why
                # the apparent terminal frame did not revoke approval routing;
                # emit at most once for the whole run.
                if not active.completion_deferral_observed:
                    active.completion_deferral_observed = True
                    logger.info(
                        "Deferring Claude run completion while %d delegated task(s) remain",
                        len(active.result_deferring_tasks),
                    )
            else:
                self._complete_turn(active, message)

    @staticmethod
    def _delegated_task_event(
        active: _ActiveTurn,
        *,
        activity: Literal["started", "updated", "terminal"],
        now: float,
    ) -> DelegatedTaskLifecycleEvent:
        oldest_age = None
        if active.result_deferring_tasks:
            oldest_age = max(0.0, now - min(active.result_deferring_tasks.values()))
        return DelegatedTaskLifecycleEvent(
            active_count=len(active.result_deferring_tasks),
            oldest_age_seconds=oldest_age,
            activity=activity,
        )

    @staticmethod
    def _delegated_task_change(
        message: Message,
    ) -> tuple[Literal["started", "updated", "terminal"], str, object | None] | None:
        """Normalize one SDK task frame without retaining any task body."""
        if isinstance(message, TaskStartedMessage):
            task_id = _normalize_task_id(message.task_id)
            return ("started", task_id, message.task_type) if task_id is not None else None
        if isinstance(message, TaskNotificationMessage):
            task_id = _normalize_task_id(message.task_id)
            return ("terminal", task_id, None) if task_id is not None else None
        if isinstance(message, TaskUpdatedMessage):
            task_id = _normalize_task_id(message.task_id)
            if task_id is None:
                return None
            status = ClaudeSession._task_update_status(message)
            typed_action: Literal["updated", "terminal"] = (
                "terminal" if status in SDK_TERMINAL_TASK_STATUSES else "updated"
            )
            return (typed_action, task_id, None)
        if not isinstance(message, SystemMessage):
            return None
        data = message.data
        task_id = _normalize_task_id(data.get("task_id"))
        if task_id is None:
            return None
        if message.subtype == "task_started":
            return ("started", task_id, data.get("task_type"))
        if message.subtype == "task_notification":
            return ("terminal", task_id, None)
        if message.subtype == "task_updated":
            patch = data.get("patch")
            status = patch.get("status") if isinstance(patch, Mapping) else None
            system_action: Literal["updated", "terminal"] = (
                "terminal" if status in SDK_TERMINAL_TASK_STATUSES else "updated"
            )
            return (system_action, task_id, None)
        return None

    @staticmethod
    def _observe_result_deferring_task(
        active: _ActiveTurn,
        message: Message,
    ) -> DelegatedTaskLifecycleEvent | None:
        """Mirror the Agent SDK's run-boundary task ledger without task bodies."""

        change = ClaudeSession._delegated_task_change(message)
        if change is None:
            return None
        action, task_id, task_type = change
        now = asyncio.get_running_loop().time()
        if action == "started":
            if (
                task_type not in _RESULT_DEFERRING_TASK_TYPES
                or task_id in active.result_deferring_terminal_tasks
                or task_id in active.result_deferring_tasks
            ):
                return None
            active.result_deferring_tasks[task_id] = now
            return ClaudeSession._delegated_task_event(active, activity="started", now=now)
        if action == "terminal":
            active.result_deferring_terminal_tasks.add(task_id)
            if active.result_deferring_tasks.pop(task_id, None) is None:
                return None
            return ClaudeSession._delegated_task_event(active, activity="terminal", now=now)
        if task_id not in active.result_deferring_tasks:
            return None
        return ClaudeSession._delegated_task_event(active, activity="updated", now=now)

    def _observe_session_id(self, message: Message) -> None:
        if self._session_id is not None:
            return
        candidate: object
        if isinstance(message, SystemMessage):
            candidate = message.data.get("session_id")
        else:
            candidate = getattr(message, "session_id", None)
        if isinstance(candidate, str) and candidate:
            self._session_id = candidate
            self._session_ready.set()

    async def _handle_unsolicited_frame(self, message: Message) -> None:
        """Consume one between-turns ("unsolicited") SDK frame.

        Assistant text is buffered until its terminal ResultMessage so the
        registered handler receives one complete message per autonomous turn,
        not one per SDK frame. StreamEvent partials only establish ownership;
        they are never delivered (no live draft exists for an unsolicited
        turn). Without a registered handler the terminal frame is dropped —
        the adapter's pre-seam behavior.
        """

        if self._unsolicited_discard:
            # Late frames of an abandoned send_turn: swallow everything
            # through the abandoned turn's terminal ResultMessage so its
            # answer cannot deliver twice.
            if isinstance(message, ResultMessage):
                self._unsolicited_discard = False
                self._unsolicited_inflight = False
                self._unsolicited_texts.clear()
                logger.warning(
                    "Swallowed late Claude ResultMessage after an abandoned turn: "
                    "session=%s",
                    message.session_id,
                )
            return
        if isinstance(message, StreamEvent):
            # The first token delta establishes turn ownership even though
            # unsolicited partials are intentionally not delivered.
            self._unsolicited_inflight = True
            return
        if isinstance(message, AssistantMessage):
            self._unsolicited_inflight = True
            self._unsolicited_texts.extend(
                block.text for block in message.content if isinstance(block, TextBlock)
            )
            return
        if not isinstance(message, ResultMessage):
            return
        raw = message.result or "\n".join(self._unsolicited_texts)
        self._unsolicited_texts.clear()
        self._unsolicited_inflight = False
        handler = self._unsolicited_handler
        if handler is None:
            logger.warning(
                "Dropping unsolicited Claude result without a registered handler: "
                "session=%s",
                message.session_id,
            )
            return
        try:
            await handler(raw, message.session_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Fail-open: a broken delivery route must never take down the
            # reader task that also serves in-turn frames.
            logger.exception("Unsolicited Claude delivery handler failed")

    @staticmethod
    def _route_stream_event(active: _ActiveTurn, message: StreamEvent) -> None:
        # Only top-level assistant text streams; nested subagent deltas carry
        # parent_tool_use_id and must not pollute the turn's answer text.
        if message.parent_tool_use_id is not None:
            return
        delta = _extract_stream_text_delta(message.event)
        if delta:
            active.streamed_current_message = True
            active.emitted_text = True
            active.queue.put_nowait(TextDeltaEvent(delta))

    def _route_assistant_message(self, active: _ActiveTurn, message: AssistantMessage) -> None:
        for block in message.content:
            if isinstance(block, TextBlock):
                # Whole-block fallback only when token deltas did not already
                # stream this message; otherwise the text would be doubled.
                if block.text and not active.streamed_current_message:
                    active.emitted_text = True
                    active.queue.put_nowait(TextDeltaEvent(block.text))
            elif isinstance(block, ThinkingBlock):
                if block.thinking:
                    active.queue.put_nowait(ReasoningDeltaEvent(block.thinking))
            elif isinstance(block, (ToolUseBlock, ServerToolUseBlock)):
                # A tool in the same assistant message proves any preceding
                # user-visible text was interim; close that message first.
                self._flush_message_boundary(active)
                active.open_tools[block.id] = block.name
                active.queue.put_nowait(
                    ToolStartedEvent(
                        block.id,
                        block.name,
                        cast(Mapping[str, JsonValue], block.input),
                    )
                )
            elif isinstance(block, ServerToolResultBlock):
                self._emit_tool_completed(
                    active,
                    block.tool_use_id,
                    cast(JsonValue, block.content),
                    success=True,
                )
        # Each completed SDK assistant message is a message boundary.
        self._flush_message_boundary(active)
        active.streamed_current_message = False

    def _route_tool_results(self, active: _ActiveTurn, message: UserMessage) -> None:
        content = message.content
        if not isinstance(content, list):
            return
        for block in content:
            if isinstance(block, ToolResultBlock):
                self._emit_tool_completed(
                    active,
                    block.tool_use_id,
                    cast(JsonValue, block.content),
                    success=block.is_error is not True,
                )

    def _emit_tool_completed(
        self,
        active: _ActiveTurn,
        tool_call_id: str,
        result: JsonValue,
        *,
        success: bool,
    ) -> None:
        # Pair by call id: a completion without a recorded start (or a second
        # completion for the same id) never reaches the stream.
        tool_name = active.open_tools.pop(tool_call_id, None)
        if tool_name is None:
            return
        if tool_name == "Bash" and success:
            self._observe_background_task_result(result)
        active.queue.put_nowait(
            ToolCompletedEvent(tool_call_id, tool_name, result, success)
        )

    @staticmethod
    def _flush_message_boundary(active: _ActiveTurn) -> None:
        if active.emitted_text:
            active.queue.put_nowait(MessageCompletedEvent())
            active.emitted_text = False

    def _complete_turn(self, active: _ActiveTurn, message: ResultMessage) -> None:
        if active.interrupt_requested:
            active.queue.put_nowait(
                ErrorEvent(INTERRUPTED_ERROR_CODE, "Claude turn was interrupted")
            )
        elif message.is_error:
            # Include diagnostic fields (subtype/api_error_status/terminal_reason)
            # in the user-facing message when result is empty, per #901.
            text = (message.result or "").strip()
            if not text:
                parts = ["Claude turn failed"]
                if message.subtype and message.subtype != "success":
                    parts.append(f"(subtype: {message.subtype})")
                if message.api_error_status:
                    parts.append(f"(HTTP status: {message.api_error_status})")
                if message.terminal_reason:
                    parts.append(f"(reason: {message.terminal_reason})")
                text = " ".join(parts)
            active.queue.put_nowait(
                ErrorEvent(
                    self._error_code(message.subtype),
                    text,
                    retryable=message.api_error_status in _RETRYABLE_HTTP_STATUSES,
                )
            )
        else:
            self._flush_message_boundary(active)
            active.queue.put_nowait(self._result_event(message))
            active.queue.put_nowait(CompletionEvent(message.stop_reason or "end_turn"))
        active.finished = True

    @staticmethod
    def _error_code(subtype: str) -> str:
        if subtype and subtype != "success" and _SNAKE_CASE_CODE.match(subtype):
            return subtype
        return "claude_turn_failed"

    @staticmethod
    def _result_event(message: ResultMessage) -> ResultEvent:
        payload: dict[str, JsonValue] = {
            "subtype": message.subtype,
            "result": message.result,
            "session_id": message.session_id,
            "duration_ms": message.duration_ms,
            "num_turns": message.num_turns,
            "total_cost_usd": message.total_cost_usd,
            "usage": cast(JsonValue, message.usage),
        }
        try:
            return ResultEvent(payload)
        except (TypeError, ValueError):
            # Never let a non-JSON usage payload swallow the terminal event.
            return ResultEvent({"subtype": message.subtype, "result": message.result})

    def _fail_active_turn(self, code: str, message: str) -> None:
        active = self._active_turn
        if active is None or active.finished:
            return
        active.queue.put_nowait(ErrorEvent(code, message or "Claude connection failed"))
        active.finished = True

    # -- approvals ----------------------------------------------------------

    async def _handle_permission_request(
        self,
        tool_name: str,
        tool_input: dict[str, object],
        context: ToolPermissionContext,
    ) -> PermissionResult:
        """SDK ``can_use_tool`` callback bridged to the per-turn approval handler.

        Fail-closed: without an in-flight turn, or when the turn's handler
        raises, the provider receives a deny decision.
        """

        active = self._active_turn
        if active is None or active.finished:
            logger.info(
                "Approval request denied (no active route) provider=claude "
                "tool=%s target_kind=%s request_id=%s turn=none outcome=denied-no-route",
                tool_name,
                _approval_target_kind(tool_input),
                getattr(context, "tool_use_id", None),
            )
            return PermissionResultDeny(message=_NO_ACTIVE_APPROVAL_ROUTE)
        generation = active.generation
        self._approval_counter += 1
        request_id = context.tool_use_id or f"approval-{self._approval_counter}"
        request = ApprovalRequestEvent(
            request_id=request_id,
            action=tool_name,
            arguments=cast(Mapping[str, JsonValue], tool_input),
            description=context.title or f"Claude requests permission to use {tool_name}",
        )
        active.queue.put_nowait(request)
        # #1045: a fail-closed deny used to be indistinguishable from an
        # explicit handler deny — one generic message, one log shape. Headless
        # (external_event) turns hit exactly these branches, so every deny now
        # carries its decision point as a body-free reason code, in both the
        # agent-visible message and the INFO trace. Never the request content.
        deny_reason: str | None = None
        try:
            decision = await active.approval_handler(request)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            decision = ApprovalDecision.DENY
            deny_reason = "handler-exception"
            logger.warning(
                "Claude approval handler raised %s; denying (fail-closed) "
                "request_id=%s",
                type(exc).__name__,
                request_id,
            )
        if (
            active.finished
            or self._turn_generation != generation
            or self._active_turn is not active
        ):
            decision = ApprovalDecision.DENY
            deny_reason = deny_reason or "turn-superseded"
        outcome = "allowed" if decision is ApprovalDecision.ALLOW else "denied"
        if decision is not ApprovalDecision.ALLOW:
            deny_reason = deny_reason or "handler-deny"
        logger.info(
            "Approval request provider=claude tool=%s target_kind=%s "
            "request_id=%s turn=active outcome=%s reason=%s",
            tool_name,
            _approval_target_kind(tool_input),
            request_id,
            outcome,
            deny_reason or "-",
        )
        if decision is ApprovalDecision.ALLOW:
            return PermissionResultAllow()
        return PermissionResultDeny(
            message=(
                "Denied by the bridge approval handler "
                f"(reason={deny_reason}; deny trace is in the bridge log)"
            )
        )


class ClaudeRuntime:
    """AgentRuntime over per-session ``ClaudeSDKClient`` connections."""

    def __init__(
        self,
        *,
        sdk_client_factory: SdkClientFactory | None = None,
        settings: Any = None,
        transcripts_dir: str | Path | None = None,
        session_id_timeout_seconds: float = 30.0,
    ) -> None:
        if session_id_timeout_seconds <= 0:
            raise ValueError("Claude session id timeout must be positive")
        self._sdk_client_factory = sdk_client_factory or _default_sdk_client_factory
        self._transcripts_dir = Path(transcripts_dir) if transcripts_dir is not None else None
        self._session_id_timeout_seconds = session_id_timeout_seconds
        # Execution-profile wiring (#623): with bound bridge settings the
        # adapter regains the boundary the retired direct stream path built
        # from tool_policy / curated_memory / web_mcp. Without settings (bare
        # construction in unit tests and the conformance harness) options
        # carry only the request-derived fields, as before.
        self._settings = settings
        # Resolved once here (not inside the settings-bound branch below) so
        # the bare, settings-free adapter also gets an explicit bound instead
        # of the SDK's 1 MiB fallback.
        self._max_buffer_size = _resolve_max_buffer_size(settings)
        self._execution_profile: str | None = None
        self._bash_policy: str | None = None
        self._claude_unrestricted = False
        if settings is not None:
            self._execution_profile = resolve_execution_profile(
                getattr(settings, "execution_profile", EXECUTION_STRICT_PROJECT),
                allowed_user_ids=getattr(settings, "allowed_user_ids", []),
                require_allowlist=getattr(settings, "require_allowlist", True),
            )
            self._bash_policy = effective_bash_policy(
                resolve_bash_policy(getattr(settings, "bash_policy", None)),
                self._execution_profile,
            )
            self._claude_unrestricted = claude_unrestricted_enabled(
                getattr(settings, "claude_unrestricted", False),
                self._execution_profile,
                is_root=running_as_root(),
            )
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._turn_owners: dict[str, ClaudeSession] = {}
        self._sessions: list[ClaudeSession] = []
        self._closed = False

    def _session_lock(self, session_id: str) -> asyncio.Lock:
        return self._session_locks.setdefault(session_id, asyncio.Lock())

    def _forget_session(self, session: ClaudeSession) -> None:
        """Drop a closed conversation session from the runtime registry."""

        try:
            self._sessions.remove(session)
        except ValueError:
            pass

    # -- AgentRuntime protocol ---------------------------------------------

    async def start_or_resume(self, request: SessionRequest) -> ClaudeSession:
        if self._closed:
            raise RuntimeError("Claude runtime is closed")
        # In streaming-input mode the real SDK does not emit the initial
        # system frame until the first query is written.  The neutral runtime
        # must return a stable session id before ``send_turn`` can be called,
        # so allocate the UUID here and ask Claude Code to use it rather than
        # waiting for a frame that cannot arrive yet.
        session_id = request.session_id or str(uuid.uuid4())
        session = ClaudeSession(self, session_id)
        options = self._build_options(
            request,
            session._handle_permission_request,
            stderr=session._record_stderr,
        )
        if request.session_id is None:
            options.session_id = session_id
        client = self._sdk_client_factory(options)
        await session._start(client, timeout_seconds=self._session_id_timeout_seconds)
        self._sessions.append(session)
        return session

    async def list_models(self) -> Sequence[ModelInfo]:
        return CURATED_CLAUDE_MODELS

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        sessions = tuple(self._sessions)
        self._sessions.clear()
        for session in sessions:
            await session.close()

    def _build_options(
        self,
        request: SessionRequest,
        can_use_tool: CanUseTool,
        *,
        stderr: Callable[[str], None] | None = None,
    ) -> ClaudeAgentOptions:
        # Fail closed on request fields this adapter cannot express through
        # the SDK: silently dropping a policy would weaken the boundary the
        # caller asked for.
        if request.sandbox_policy is not None:
            raise ValueError("Claude runtime does not support sandbox policies yet")
        if request.approvals_reviewer is not None:
            raise ValueError("Claude runtime does not support approvals reviewers")
        permission_mode: PermissionMode | None = None
        if request.approval_policy is not None:
            if request.approval_policy not in _PERMISSION_MODES:
                raise ValueError(
                    f"unsupported Claude approval policy: {request.approval_policy!r}"
                )
            permission_mode = cast(PermissionMode, request.approval_policy)
        effort: EffortLevel | None = None
        if request.effort is not None:
            if request.effort not in _EFFORT_LEVELS:
                raise ValueError(f"unsupported Claude effort: {request.effort!r}")
            effort = cast(EffortLevel, request.effort)
        options = ClaudeAgentOptions(
            cwd=request.working_directory,
            model=request.model,
            resume=request.session_id,
            permission_mode=permission_mode,
            effort=effort,
            can_use_tool=can_use_tool,
            include_partial_messages=True,
            stderr=stderr,
            # Always explicit: None here means the SDK's 1 MiB NDJSON line
            # limit, which an image-bearing tool result overflows and kills
            # the reader task for the rest of the turn (incident 2026-08-03).
            max_buffer_size=self._max_buffer_size,
        )
        if self._settings is not None:
            self._apply_execution_profile(options, request)
            if getattr(self._settings, "memory_distill_provider", "auto") != "off":
                options.env = {
                    **(dict(options.env) if options.env is not None else {}),
                    "CCC_BRIDGE_DISTILL_MANAGED": "1",
                }
        return options

    def _apply_execution_profile(
        self, options: ClaudeAgentOptions, request: SessionRequest
    ) -> None:
        """Wire the execution-profile builders into the adapter options (#623).

        Mirrors the reference wiring of the legacy ``_create_user_stream``
        (removed in #584 C-2): the tool_policy permission bundle (allow/deny
        lists plus per-call ask hooks feeding ``can_use_tool``), curated web
        MCP routing, setting_sources control, curated memory injection, and
        the strict-project OS Bash sandbox.
        """

        settings = self._settings
        permission_options = sdk_permission_options(self._bash_policy)
        allowed_tools = list(permission_options["allowed_tools"])
        disallowed_tools = list(permission_options["disallowed_tools"])
        options.hooks = {
            event: list(matchers) for event, matchers in permission_options["hooks"].items()
        }
        web_mcp = build_curated_web_mcp(settings)
        if web_mcp is not None:
            allowed_tools = [
                tool for tool in allowed_tools if tool not in web_mcp["disallowed_tools"]
            ] + web_mcp["allowed_tools"]
            disallowed_tools = list(
                dict.fromkeys(disallowed_tools + web_mcp["disallowed_tools"])
            )
            options.mcp_servers = web_mcp["mcp_servers"]
            options.env = dict(web_mcp["process_env"])
            options.system_prompt = web_mcp["system_prompt"]
        options.allowed_tools = allowed_tools
        options.disallowed_tools = disallowed_tools
        if self._execution_profile == EXECUTION_OWNER_OPERATOR:
            if self._claude_unrestricted:
                # Opt-in Codex parity (owner-operator only): bypass permission
                # checks and drop the host settings chain, preserving memory
                # context through the curated settings block.
                options.permission_mode = "bypassPermissions"
                options.setting_sources = []
                self._apply_curated_memory(options, request)
            elif (
                getattr(settings, "bridge_memory_mode", MEMORY_MODE_OFF)
                == MEMORY_MODE_AUDIENCE_SCOPED
            ):
                # Audience isolation is stronger than owner-operator's normal
                # host-settings convenience. Loading the global user/project
                # settings chain here could re-register unscoped memory hooks.
                options.setting_sources = []
                self._apply_curated_memory(options, request)
            else:
                # Owner-operated bridges intentionally retain host utility and
                # the normal Claude Code settings/context chain.
                options.setting_sources = ["user", "project", "local"]
            return
        # Every non-owner profile suppresses filesystem settings. Even when
        # Bash is disallowed, user/project/local settings can register host
        # shell hooks independently of the model-facing Bash tool.
        options.setting_sources = []
        self._apply_curated_memory(options, request)
        if (
            self._execution_profile == EXECUTION_STRICT_PROJECT
            and self._bash_policy != BASH_DISABLED
        ):
            # Strict-project uses the SDK OS sandbox as the Bash boundary.
            cli_path = getattr(settings, "claude_cli_path", None)
            options.sandbox = cast(
                SandboxSettings,
                strict_bash_sandbox_settings(
                    Path(request.working_directory),
                    str(cli_path) if cli_path else None,
                ),
            )

    def _apply_curated_memory(
        self, options: ClaudeAgentOptions, request: SessionRequest
    ) -> None:
        """Attach only the canonical memory route resolved for this request."""

        mode = getattr(self._settings, "bridge_memory_mode", MEMORY_MODE_OFF)
        audience = None
        if mode == MEMORY_MODE_AUDIENCE_SCOPED:
            audience = audience_from_claude_environment(
                self._settings,
                request.memory_environment,
            )
        elif request.memory_environment is not None:
            raise ValueError(
                "Claude memory environment requires audience-scoped bridge memory"
            )
        curated_settings = build_curated_memory_settings(
            self._settings,
            audience=audience,
        )
        if curated_settings is not None:
            options.settings = curated_settings

    # -- SessionBrowser protocol -------------------------------------------

    @property
    def supports_session_browsing(self) -> bool:
        return self._transcripts_dir is not None

    async def list_sessions(self, *, limit: int = 10) -> Sequence[SessionSummary]:
        """List stored SDK transcripts newest-first, bounded and normalized."""

        directory = self._transcripts_dir
        if limit <= 0 or directory is None or not directory.is_dir():
            return ()
        bounded_limit = min(limit, 100)
        candidates: list[tuple[float, Path]] = []
        for path in directory.glob("*.jsonl"):
            try:
                candidates.append((path.stat().st_mtime, path))
            except OSError:
                continue
        candidates.sort(key=lambda entry: entry[0], reverse=True)
        summaries: list[SessionSummary] = []
        for mtime, path in candidates[:bounded_limit]:
            summaries.append(
                SessionSummary(
                    id=path.stem,
                    preview=self._first_user_preview(path),
                    updated_at=mtime,
                )
            )
        return tuple(summaries)

    async def read_session(self, session_id: str, *, limit: int = 5) -> SessionHistory:
        """Return bounded user/assistant text from one stored transcript."""

        if not session_id:
            raise ValueError("session id must not be empty")
        directory = self._transcripts_dir
        if limit <= 0 or directory is None or not _SAFE_SESSION_ID.match(session_id):
            return SessionHistory(session_id, ())
        path = directory / f"{session_id}.jsonl"
        messages: list[SessionHistoryMessage] = []
        for _index, role, content, timestamp in iter_transcript_messages(path):
            text = _first_text_block(content)[:2000].strip()
            if not text:
                continue
            if role == "user":
                messages.append(SessionHistoryMessage("user", text, timestamp or None))
            elif role == "assistant":
                messages.append(SessionHistoryMessage("assistant", text, timestamp or None))
        return SessionHistory(session_id, tuple(messages[-min(limit, 50):]))

    async def read_session_snapshot(
        self,
        session_id: str,
        *,
        bounds: TranscriptBounds,
        memory_audience: str | None = None,
        memory_scope: str | None = None,
    ) -> CodexTranscriptSnapshot:
        """Read one Claude transcript through the shared distill snapshot seam."""

        validate_memory_route(memory_audience, memory_scope)
        if self._transcripts_dir is None or not _SAFE_SESSION_ID.fullmatch(session_id):
            raise ValueError("Claude snapshot session route is unavailable")
        return await asyncio.to_thread(
            read_claude_snapshot,
            self._transcripts_dir,
            session_id,
            bounds=bounds,
        )

    @staticmethod
    def _first_user_preview(path: Path) -> str | None:
        for scanned, (_index, _role, content, _timestamp) in enumerate(
            iter_transcript_messages(path, types=("user",))
        ):
            if scanned >= _PREVIEW_SCAN_LIMIT:
                return None
            text = _first_text_block(content).strip()
            if text and not text.startswith("<"):
                return text[:100]
        return None
