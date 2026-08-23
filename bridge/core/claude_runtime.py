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
from typing import Any, Protocol
import uuid

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    Message,
)

from telegram_bot.runtime_config_check import (
    DEFAULT_CLAUDE_MAX_BUFFER_SIZE,
    MAX_CLAUDE_MAX_BUFFER_SIZE,
    MIN_CLAUDE_MAX_BUFFER_SIZE,
)
from .agent_runtime import (
    AgentEvent,
    ApprovalHandler,
    ModelInfo,
    SessionRequest,
)
from .claude_runtime_options import ClaudeRuntimeOptionsMixin
from .claude_session_approvals import (
    ClaudeSessionApprovalMixin,
    _NO_ACTIVE_APPROVAL_ROUTE as _SESSION_NO_ACTIVE_APPROVAL_ROUTE,
    _approval_target_kind as _session_approval_target_kind,
)
from .claude_session_browser import ClaudeSessionBrowserMixin
from .claude_session_delegated_tasks import ClaudeSessionDelegatedTasksMixin
from .claude_session_frame_routing import ClaudeSessionFrameRoutingMixin
from .claude_session_task_tracking import ClaudeSessionTaskTrackingMixin
from .claude_session_turn_admission import ClaudeSessionTurnAdmissionMixin
from .claude_session_turn_events import (
    ClaudeSessionTurnEventsMixin,
    INTERRUPTED_ERROR_CODE as _TURN_INTERRUPTED_ERROR_CODE,
)
from .tool_policy import (
    EXECUTION_STRICT_PROJECT,
    claude_unrestricted_enabled,
    effective_bash_policy,
    resolve_bash_policy,
    resolve_execution_profile,
    running_as_root,
)

logger = logging.getLogger(__name__)

INTERRUPTED_ERROR_CODE = _TURN_INTERRUPTED_ERROR_CODE

# Private compatibility aliases retained for focused tests and downstream
# diagnostics that already import these helpers from ``claude_runtime``.
_NO_ACTIVE_APPROVAL_ROUTE = _SESSION_NO_ACTIVE_APPROVAL_ROUTE
_approval_target_kind = _session_approval_target_kind

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


class ClaudeSession(
    ClaudeSessionTurnAdmissionMixin,
    ClaudeSessionApprovalMixin,
    ClaudeSessionDelegatedTasksMixin,
    ClaudeSessionTaskTrackingMixin,
    ClaudeSessionFrameRoutingMixin,
    ClaudeSessionTurnEventsMixin,
):
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
        # #1045 proposal 1: extra state dirs (audience-scoped route) whose
        # working-state.md is this session's checkpoint contract file.
        self._contract_state_dirs: tuple[str, ...] = ()
        # A shared-audience session may write only its scoped contract file;
        # the node's unscoped checkpoint stays private input (#1155 parity).
        self._contract_include_default = True
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

class ClaudeRuntime(ClaudeSessionBrowserMixin, ClaudeRuntimeOptionsMixin):
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
        if request.memory_environment is not None:
            scoped_state_dir = request.memory_environment.get("CCC_STATE_DIR")
            if isinstance(scoped_state_dir, str) and scoped_state_dir.strip():
                session._contract_state_dirs = (scoped_state_dir.strip(),)
            if request.memory_environment.get("CCC_MEMORY_AUDIENCE") == "shared":
                session._contract_include_default = False
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
