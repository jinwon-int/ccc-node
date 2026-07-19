"""Provider-neutral runtime adapter for the Claude Agent SDK (#584 slice A).

``ClaudeRuntime`` implements the ``AgentRuntime`` protocol from
``core.agent_runtime`` on top of ``ClaudeSDKClient``.  It is additive: the
live Telegram path still runs through ``project_chat`` and switches to this
adapter only at the flagged cutover.  The SDK-frame -> event translation
mirrors the semantics of ``project_chat_reader._reader_loop`` (text deltas,
message boundaries, tool lifecycle, terminal results) re-expressed as the
normalized ``AgentEvent`` stream that the runtime conformance suite pins.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
import logging
from pathlib import Path
import re
from typing import Protocol, cast

from claude_agent_sdk import (
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
    TextBlock,
    ThinkingBlock,
    ToolPermissionContext,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from .agent_runtime import (
    AgentEvent,
    ApprovalDecision,
    ApprovalHandler,
    ApprovalRequestEvent,
    CompletionEvent,
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
from .project_chat_history import _first_text_block, iter_transcript_messages
from .sdk_text import _extract_stream_text_delta

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

# The Claude CLI resolves these aliases itself; the bridge's /model surface is
# the same static curated set (model_discovery stays a curated list until the
# SDK exposes provider-side enumeration).
CURATED_CLAUDE_MODELS: tuple[ModelInfo, ...] = (
    ModelInfo(id="sonnet", display_name="Claude Sonnet", is_default=True),
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

# Optional seam mirroring the direct path's ``UnsolicitedCallback``
# (project_chat_types): async (text, session_id) -> None. Delivers assistant
# output produced outside any ``send_turn`` (for example the CLI autonomously
# continuing after a harness background-task notification).
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


@dataclass(slots=True)
class _ActiveTurn:
    queue: asyncio.Queue[AgentEvent]
    approval_handler: ApprovalHandler
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


class ClaudeSession:
    """One provider-neutral session backed by a dedicated ``ClaudeSDKClient``."""

    def __init__(self, runtime: ClaudeRuntime, requested_session_id: str | None) -> None:
        self._runtime = runtime
        self._session_id: str | None = requested_session_id
        self._session_ready = asyncio.Event()
        self._client: SdkClient | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._turn_lock: asyncio.Lock | None = None
        self._active_turn: _ActiveTurn | None = None
        self._approval_counter = 0
        self._closed = False
        # Between-turns ("unsolicited") frame state, mirroring the direct
        # path's ``_UserStreamState`` machinery (project_chat_reader):
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
        if self._closed:
            return
        self._closed = True
        self._fail_active_turn("claude_runtime_closed", "Claude runtime closed")
        if self._reader_task is not None:
            self._reader_task.cancel()
            await asyncio.gather(self._reader_task, return_exceptions=True)
            self._reader_task = None
        if self._client is not None:
            try:
                await self._client.disconnect()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Claude SDK client disconnect failed during close")

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
                active = _ActiveTurn(asyncio.Queue(), approval_handler)
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

        return events()

    async def interrupt(self) -> None:
        active = self._active_turn
        client = self._client
        if active is None or active.finished or client is None:
            return
        active.interrupt_requested = True
        await client.interrupt()

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
        active = self._active_turn
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
            self._complete_turn(active, message)

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
        """Consume one between-turns SDK frame (direct-path unsolicited mirror).

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

    @staticmethod
    def _emit_tool_completed(
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
            text = (message.result or "").strip() or "Claude turn failed"
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
            return PermissionResultDeny(message="No active turn accepts approval requests")
        self._approval_counter += 1
        request_id = context.tool_use_id or f"approval-{self._approval_counter}"
        request = ApprovalRequestEvent(
            request_id=request_id,
            action=tool_name,
            arguments=cast(Mapping[str, JsonValue], tool_input),
            description=context.title or f"Claude requests permission to use {tool_name}",
        )
        active.queue.put_nowait(request)
        try:
            decision = await active.approval_handler(request)
        except asyncio.CancelledError:
            raise
        except Exception:
            decision = ApprovalDecision.DENY
        if active.finished or self._active_turn is not active:
            decision = ApprovalDecision.DENY
        if decision is ApprovalDecision.ALLOW:
            return PermissionResultAllow()
        return PermissionResultDeny(message="Denied by the bridge approval handler")


class ClaudeRuntime:
    """AgentRuntime over per-session ``ClaudeSDKClient`` connections."""

    def __init__(
        self,
        *,
        sdk_client_factory: SdkClientFactory | None = None,
        transcripts_dir: str | Path | None = None,
        session_id_timeout_seconds: float = 30.0,
    ) -> None:
        if session_id_timeout_seconds <= 0:
            raise ValueError("Claude session id timeout must be positive")
        self._sdk_client_factory = sdk_client_factory or _default_sdk_client_factory
        self._transcripts_dir = Path(transcripts_dir) if transcripts_dir is not None else None
        self._session_id_timeout_seconds = session_id_timeout_seconds
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._sessions: list[ClaudeSession] = []
        self._closed = False

    def _session_lock(self, session_id: str) -> asyncio.Lock:
        return self._session_locks.setdefault(session_id, asyncio.Lock())

    # -- AgentRuntime protocol ---------------------------------------------

    async def start_or_resume(self, request: SessionRequest) -> ClaudeSession:
        if self._closed:
            raise RuntimeError("Claude runtime is closed")
        session = ClaudeSession(self, request.session_id)
        options = self._build_options(request, session._handle_permission_request)
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
        for session in self._sessions:
            await session.close()
        self._sessions.clear()

    def _build_options(
        self, request: SessionRequest, can_use_tool: CanUseTool
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
        return ClaudeAgentOptions(
            cwd=request.working_directory,
            model=request.model,
            resume=request.session_id,
            permission_mode=permission_mode,
            effort=effort,
            can_use_tool=can_use_tool,
            include_partial_messages=True,
        )

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
