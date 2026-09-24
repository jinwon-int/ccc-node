"""Message submission/retry mixin for ProjectChatHandler."""

# mypy: disable-error-code="attr-defined"

import asyncio
from dataclasses import dataclass
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, Optional

from claude_agent_sdk import RateLimitEvent, ResultMessage

from telegram_bot.core.agent_runtime import (
    AgentEvent,
    ApprovalDecision,
    ApprovalRequestEvent,
    JsonValue as AgentJsonValue,
    SessionRequest,
    TaskProgressEvent,
)
from telegram_bot.core.danso_worker import TASK_RESUME_CONTROL
from telegram_bot.core.memory_audience import resolve_memory_audience
from telegram_bot.core.agent_session_registry import ActiveToken
from telegram_bot.core.external_wait import clear_active_turn, publish_active_turn
from telegram_bot.core.project_chat_types import (
    AgentApprovalCallback,
    AgentSessionEntry,
    ChatResponse,
    InterimMessageCallback,
    PermissionCallback,
    StatusCallback,
    TypingCallback,
    _PendingRequest,
)
from telegram_bot.core.project_chat_output import TurnOutputBuffer
from telegram_bot.core.project_chat_request_progress import (
    RequestProgressCoordinator,
    RequestProgressHandle,
)
from telegram_bot.core.project_chat_turn_consumer import (
    TurnEventDirective,
    TurnStreamOutcome,
    consume_turn_stream,
)
from telegram_bot.core.project_chat_turn_state import (
    DelegatedTaskLifecycleTransition,
    ErrorTransition,
    IgnoredTransition,
    MessageCompletedTransition,
    ResultTransition,
    TextDeltaTransition,
    ToolCompletedTransition,
    ToolStartedTransition,
    TurnEventTransition,
    TurnEventState,
)
from telegram_bot.core.request_lifecycle import (
    RequestPhase,
    TerminalAttemptKind,
)
from telegram_bot.core.usage import claude_endpoint_host
from telegram_bot.core.usage_meter import MODE_INTERACTIVE
from telegram_bot.core.sdk_text import TERMINAL_STALL_NOTICE
from telegram_bot.core.skill_advice import advise_turn
from telegram_bot.utils.chat_logger import log_chat
from telegram_bot.core.codex_app_server import CodexConnectionClosedError
from telegram_bot.utils.health import health_reporter

logger = logging.getLogger(__name__)

_DRAIN_RESPONSE = "Bridge restart is draining existing work; please retry shortly."


def _elapsed_since(loop: Any, request: _PendingRequest) -> str:
    """Seconds since the request was registered, or ``"?"`` if unknowable.

    Deliberately wider than the admission deadline: ``started_at`` includes
    session start and turn-lock wait, so a value far above the grace says the
    turn was queued behind something, while a value at the grace says the
    provider itself went quiet. Reporting only the grace hides that difference.
    """
    try:
        started_at = getattr(request, "started_at", None)
        if started_at is None:
            return "?"
        return f"{loop.time() - float(started_at):.1f}"
    except Exception:  # pragma: no cover - defensive
        return "?"


def _admission_diagnostics(
    session: Any,
    *,
    provider: object,
    model: object,
    elapsed: str,
    grace: float,
) -> dict[str, object]:
    """Body-free account of a turn that never produced a first event.

    Every field is either a bounded label or a number. The endpoint is a host,
    never the raw ``ANTHROPIC_BASE_URL`` (which may carry userinfo), and the
    CLI's stderr is reduced to a class by the runtime rather than quoted.

    Fail-open by construction: this runs only on a path that is already
    failing, so a diagnostic that raises would replace a useful warning with a
    traceback. ``transport_diagnostics`` is optional — Codex sessions do not
    have it, and neither do the test doubles.
    """
    details: dict[str, object] = {
        "provider": provider or "unknown",
        "model": model or "default",
        "endpoint": "unknown",
        "elapsed": elapsed,
        "grace": grace,
        "exit_code": None,
        "stderr_class": None,
        "stderr_lines": 0,
    }
    try:
        details["endpoint"] = claude_endpoint_host()
    except Exception:  # pragma: no cover - defensive
        pass
    try:
        probe = getattr(session, "transport_diagnostics", None)
        if callable(probe):
            reported = probe()
            if isinstance(reported, Mapping):
                for field in ("exit_code", "stderr_class", "stderr_lines"):
                    if field in reported:
                        details[field] = reported[field]
    except Exception:  # pragma: no cover - defensive
        pass
    return details



def _stall_ages(loop: Any, request: _PendingRequest) -> dict[str, str]:
    """Seconds since the turn's last signs of life, as strings for the log.

    The pair that matters is silence vs last_tool: text arriving AFTER the last
    tool event (last_text_age < last_tool_age) says the model finished speaking
    and the missing piece is the terminal frame — the provider's side. Text
    arriving before a newer tool event would say the turn died mid-work. The
    admission log cannot make this distinction; this one exists to.
    """
    ages: dict[str, str] = {}
    try:
        now = loop.time()
        for key, attr in (
            ("silence", "last_event_at"),
            ("last_text_age", "last_text_at"),
            ("last_tool_age", "last_tool_at"),
        ):
            stamp = float(getattr(request, attr, 0.0) or 0.0)
            ages[key] = f"{now - stamp:.1f}" if stamp > 0 else "never"
    except Exception:  # pragma: no cover - defensive
        ages = {"silence": "?", "last_text_age": "?", "last_tool_age": "?"}
    return ages


# Admission failures worth a second attempt: the provider said nothing at all
# (the shape seen on gongmyoung), or it named a transient transport fault.
# `auth`, `rate-limit`, `tls` and `oom` are deliberately absent — retrying a
# rejected key changes nothing and retrying a throttle makes it worse.
_RETRYABLE_ADMISSION_CLASSES = frozenset({"silent", "network", "timeout"})

# A provider turn that runs to a normal completion yet produces no
# user-visible text anywhere (#775). Measured on gwakga 2026-08-18 14:44: a
# follow-up "did the background job finish?" turn ended after ~7s with
# ResultEvent+CompletionEvent and zero text, and the bridge handed the
# user "Please retry your request" instead of retrying itself. The turn
# completed, so this is not an admission stall — but it is the same
# short-window transient class, and the bounded retry budget below is the
# right remedy for both.
_EMPTY_COMPLETION_MARKER = "empty-completion"


def _admission_retry_class(response: ChatResponse) -> str | None:
    """The retryable failure class of a turn, else ``None``.

    Two marker shapes are retryable: ``admission-timeout/<stderr class>``
    where the class looks transient, and a bare ``empty-completion`` turn
    the provider finished without ever speaking. The ``coalesced-turn``
    marker is deliberately excluded — the answer is already on its way
    under the follower turn, so resending only queues a duplicate.
    """
    marker = getattr(response, "failure_class", None) or ""
    if marker == _EMPTY_COMPLETION_MARKER:
        return marker
    prefix, _, cls = marker.partition("/")
    if prefix != "admission-timeout":
        return None
    return cls if cls in _RETRYABLE_ADMISSION_CLASSES else None


def _claim_request_terminal(request: _PendingRequest, phase: RequestPhase, *, cause: str) -> bool:
    """Claim a terminal result without turning normal races into exceptions."""

    attempt = request.lifecycle.try_terminal(phase, cause=cause)
    if attempt.kind is TerminalAttemptKind.LOST:
        logger.warning(
            "Request terminal race lost: wanted=%s existing=%s cause=%s",
            phase.value,
            attempt.phase.value,
            attempt.cause,
        )
    return attempt.kind is TerminalAttemptKind.WON


async def _await_offloaded_write(fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> None:
    """Run a best-effort fsync-backed write in a worker thread, cancel-safely.

    ``asyncio.to_thread`` cannot interrupt the thread, so a cancellation that
    arrives mid-write is re-raised only once the write has landed (the same
    shape as ``TimeoutPreservingEventReader._drain_pending``). That keeps a
    turn's ``publish_active_turn`` / ``clear_active_turn`` pair ordered: the
    finally-block clear can never overtake a publish still in flight and leave
    a stale route behind (#1479). The write itself is fail-open.
    """

    write = asyncio.ensure_future(asyncio.to_thread(fn, *args, **kwargs))
    interrupted: asyncio.CancelledError | None = None
    while True:
        try:
            await asyncio.shield(write)
        except asyncio.CancelledError as exc:
            interrupted = exc
            if write.done():
                break
            continue
        break
    if interrupted is not None:
        raise interrupted


async def _finalize_request_progress(
    *,
    coordinator: RequestProgressCoordinator,
    handle: RequestProgressHandle,
    session: Any | None,
    requested_session_id: str | None,
) -> None:
    """Keep lifecycle authority in the caller and delegate only ordered effects."""

    request = handle.request
    if not request.lifecycle.is_terminal:
        _claim_request_terminal(
            request,
            RequestPhase.FAILED,
            cause="finalizer-fallback",
        )
    if not request.lifecycle.begin_finalization():
        return

    terminal_outcome = request.lifecycle.terminal_outcome
    assert terminal_outcome is not None
    resolved_session_id = requested_session_id
    if session is not None:
        try:
            resolved_session_id = session.session_id
        except Exception:
            pass
    await coordinator.finalize(
        handle,
        terminal_outcome=terminal_outcome,
        session_id=resolved_session_id,
    )


def _log_user_input(
    *,
    user_message: str,
    user_id: int,
    session_id: Optional[str],
    model: Optional[str],
    sensitive_log_event: Optional[str],
) -> None:
    if sensitive_log_event is not None:
        safe_event = re.sub(r"[^a-z0-9_.-]+", "_", sensitive_log_event.lower()).strip("_")
        logger.info("Processing sensitive input event=%s", safe_event[:64] or "unknown")
        return
    logger.info("Processing message from user %s: %s...", user_id, user_message[:80])
    log_chat(user_id, session_id, "user", user_message, model=model)


def _log_approval_route_deny(
    *,
    reason: str,
    event: Optional[ApprovalRequestEvent],
    user_id: int,
    chat_id: int,
    generation: int,
) -> None:
    """Body-free deny trace for the provider-neutral approval route (#1045).

    Every fail-closed deny in ``handle_approval`` used to return a bare
    ``DENY`` with no journal line, so a headless (external_event) turn's
    denied write was unattributable — the agent saw one generic runtime
    message and the log showed nothing. Each decision point now logs its
    reason code. Only the action name and routing identifiers are logged,
    never the request arguments.
    """

    logger.info(
        "Approval route denied reason=%s action=%s request_id=%s "
        "user_id=%s chat_id=%s generation=%s",
        reason,
        getattr(event, "action", None),
        getattr(event, "request_id", None),
        user_id,
        chat_id,
        generation,
    )


@dataclass
class _TurnSessionSlot:
    """Locals that ``_acquire_turn_session`` hands back to the turn (#896 PR1).

    The inline code assigned ``session``/``turn_token`` at several points
    inside the guard lock, and the turn's ``except``/``finally`` read whatever
    value was current when control left — including after a refused
    admission or an exception mid-start (a freshly started, uncached session
    must still be dropped). The helper mirrors every assignment here and the
    caller adopts the slot in a ``finally`` so those paths stay identical.
    """

    # Typed ``Any`` on purpose: the inline code's ``session`` was inferred as
    # ``Any`` (first ``None``, then the runtime's session object) and every
    # later use relies on that; ``Optional`` here would force guards into
    # code this PR must not change.
    runtime: Any = None
    session: Any = None
    turn_token: Any = None


class _TurnCallbacks:
    """The per-turn callbacks handed to consume_turn_stream (#896 PR2).

    Built by ``_make_turn_callbacks``. ``approval_stall_won`` replaces the
    ``nonlocal`` the inline ``interrupt_turn`` closure wrote: it records
    whether the approval-stall path claimed the request's terminal phase, and
    the caller reads it once the stream has ended.
    """

    __slots__ = (
        "handle_approval",
        "deliver_pending_interim",
        "apply_agent_event",
        "interrupt_turn",
        "approval_stall_won",
    )

    handle_approval: Callable[[ApprovalRequestEvent], Awaitable[ApprovalDecision]]
    deliver_pending_interim: Callable[[], Awaitable[None]]
    apply_agent_event: Callable[[AgentEvent, float], Awaitable[TurnEventDirective]]
    interrupt_turn: Callable[[TurnStreamOutcome], Awaitable[None]]
    approval_stall_won: bool

    def __init__(self) -> None:
        self.approval_stall_won = False


class ProjectChatProcessMixin:
    def _external_wait_home(self) -> Path:
        """Durable home for external-wait registry/route files (#740)."""
        base = getattr(self._config, "bot_data_dir", None) or (
            self.project_root / ".telegram_bot"
        )
        return Path(base) / "external-wait"

    async def process_message(
        self,
        user_message: str,
        user_id: int,
        chat_id: int,
        message_id: Optional[int] = None,
        session_id: Optional[str] = None,
        model: Optional[str] = None,
        effort: Optional[str] = None,
        approval_policy: Optional[str] = None,
        approvals_reviewer: Optional[str] = None,
        sandbox_policy: Optional[Mapping[str, AgentJsonValue]] = None,
        new_session: bool = False,
        permission_callback: Optional[PermissionCallback] = None,
        approval_callback: Optional[AgentApprovalCallback] = None,
        typing_callback: Optional[TypingCallback] = None,
        status_callback: Optional[StatusCallback] = None,
        bot: Optional[Any] = None,
        notification_bot: Optional[Any] = None,
        interim_message_callback: Optional[InterimMessageCallback] = None,
        sensitive_log_event: Optional[str] = None,
        usage_mode: str = MODE_INTERACTIVE,
        resume_task: bool = False,
        dispatch_guard: Callable[[], bool] | None = None,
        streaming_sink: Optional[Any] = None,
    ) -> ChatResponse:
        del message_id
        # The legacy permission seam (perm: buttons) belonged to the removed
        # direct SDK path; the runtime path's approval boundary is
        # approval_callback. Accepted and ignored for caller compatibility.
        del permission_callback
        if getattr(self, "_shutdown_draining", False):
            return ChatResponse(
                content=f"⏳ {_DRAIN_RESPONSE}",
                success=False,
                error="bridge_draining",
                session_id=session_id,
            )
        self._require_runtime()
        # The resume marker is an internal control envelope.  A Telegram
        # message must never be allowed to reach another runtime (or a Danso
        # worker before its authorization bit is armed) merely by copying the
        # marker text.  The command route supplies both the exact marker and
        # the trusted resume_task bit below.
        if user_message == TASK_RESUME_CONTROL and not resume_task:
            return ChatResponse(
                content="❌ Invalid Danso task control.",
                success=False,
                error="danso_input",
                session_id=session_id,
            )
        provider = getattr(self._config, "agent_provider", "claude")
        if resume_task and (
            provider != "danso"
            or not getattr(self._config, "danso_long_task_enabled", False)
            or user_message != TASK_RESUME_CONTROL
            or not session_id
            or new_session
        ):
            return ChatResponse(
                content="❌ Explicit Danso long-task resume is unavailable for this conversation.",
                success=False,
                error="danso_task_resume_unavailable",
                session_id=session_id,
            )
        if provider == "claude":
            # Claude adapter path (#584): the bot layer's approval/sandbox
            # knobs are Codex app-server policies (bot_access._codex_*) that
            # ClaudeRuntime rejects fail-closed. On this path the approval
            # boundary is the SDK can_use_tool -> approval_callback seam, so
            # drop the untranslatable Codex-only knobs instead of forwarding
            # them.
            approval_policy = None
            approvals_reviewer = None
            sandbox_policy = None
        elif provider == "danso":
            # Danso uses the configured host/bubblewrap backend, independently
            # of Codex policies. Host mode has current-user OS permissions.
            approval_policy = "never"
            approvals_reviewer = None
            sandbox_policy = None
        elif provider == "piri":
            # PiriRuntime is deliberately unrestricted: built-in tools execute
            # with the bridge user's OS permissions and never pause for a
            # provider approval. Pin the only policies the adapter accepts so
            # stricter Codex UX settings cannot accidentally break Piri turns.
            approval_policy = "never"
            approvals_reviewer = None
            sandbox_policy = {"type": "dangerFullAccess"}
        if sensitive_log_event is not None:
            _log_user_input(
                user_message=user_message,
                user_id=user_id,
                session_id=session_id,
                model=model,
                sensitive_log_event=sensitive_log_event,
            )
        response = await self._process_agent_message(
            user_message=user_message,
            user_id=user_id,
            chat_id=chat_id,
            session_id=session_id,
            model=model,
            effort=effort,
            approval_policy=approval_policy,
            approvals_reviewer=approvals_reviewer,
            sandbox_policy=sandbox_policy,
            new_session=new_session,
            approval_callback=approval_callback,
            typing_callback=typing_callback,
            status_callback=status_callback,
            bot=bot,
            notification_bot=notification_bot,
            interim_message_callback=interim_message_callback,
            usage_mode=usage_mode,
            skill_advice_allowed=sensitive_log_event is None,
            resume_task=resume_task,
            dispatch_guard=dispatch_guard,
            streaming_sink=streaming_sink,
        )

        # One bounded second attempt when the provider never spoke. Retrying is
        # worth it because the failures cluster in short windows — gongmyoung's
        # uplink drops for ~60s at a time — but the retry runs on its own small
        # budget, never the full grace again: 300s + 300s is the 600s dead wait
        # that made raising the deadline the wrong fix in the first place.
        # The same budget covers `empty-completion` turns (#775): the provider
        # finished without any visible text, which is the same short-window
        # transient class seen on gwakga 2026-08-18 14:44 — the user was told
        # to retry by hand for something the bridge could retry itself.
        #
        # This does not hide the cause. The #846 warning is emitted for each
        # admission timeout, so a retried failure still leaves the first
        # attempt's provider, endpoint, exit code and stderr class in the log.
        if provider == "danso":
            return response  # Never replay a turn whose tool effects may be durable.
        retry_class = _admission_retry_class(response)
        if retry_class is None:
            return response
        attempts = int(getattr(self._config, "turn_admission_retries", 0) or 0)
        if attempts <= 0:
            return response
        retry_grace = float(
            getattr(self._config, "turn_admission_retry_timeout_seconds", 0.0) or 0.0
        )
        if retry_grace <= 0:
            return response

        logger.warning(
            "Retrying a turn with no user-visible answer for user %s chat %s "
            "(class=%s retry_grace=%gs)",
            user_id,
            chat_id,
            retry_class,
            retry_grace,
        )
        retried = await self._process_agent_message(
            user_message=user_message,
            user_id=user_id,
            chat_id=chat_id,
            session_id=session_id,
            model=model,
            effort=effort,
            approval_policy=approval_policy,
            approvals_reviewer=approvals_reviewer,
            sandbox_policy=sandbox_policy,
            new_session=new_session,
            approval_callback=approval_callback,
            typing_callback=typing_callback,
            status_callback=status_callback,
            bot=bot,
            notification_bot=notification_bot,
            interim_message_callback=interim_message_callback,
            usage_mode=usage_mode,
            resume_task=resume_task,
            dispatch_guard=dispatch_guard,
            admission_timeout_override=retry_grace,
            streaming_sink=streaming_sink,
        )
        if retried.success:
            logger.info(
                "Retry recovered the turn for user %s chat %s after a %s "
                "failure",
                user_id,
                chat_id,
                retry_class,
            )
        return retried

    async def _cancel_agent_streaming(
        self, streaming_handler: Optional[Any], *, context: str
    ) -> None:
        if streaming_handler is None:
            return
        try:
            await streaming_handler.cancel()
        except Exception:
            logger.exception("Failed to cancel agent stream while %s", context)

    def _register_agent_unsolicited_handler(
        self,
        session: Any,
        *,
        user_id: int,
        chat_id: int,
        model: Optional[str],
        route_bot: Optional[Any],
    ) -> None:
        """Route runtime-side unsolicited turns to Telegram (#584 P3-1B).

        Assistant output the runtime produced outside any active turn (for
        example the CLI autonomously continuing after a background-task
        notification) is cleaned, bounded, and sent to the same conversation.
        The seam is optional — sessions without ``set_unsolicited_handler``
        (Codex) keep their current behavior — and without a route bot any
        previously registered handler is left in place so a request that
        carries no bot never severs an existing delivery route.
        """

        setter = getattr(session, "set_unsolicited_handler", None)
        if not callable(setter):
            return
        if route_bot is None:
            return

        async def deliver_unsolicited(text: str, session_id: Optional[str]) -> None:
            content = self._clean_response(text) or "(No response)"
            payload = content
            if len(payload) > 4000:
                payload = f"{payload[:3960]}\n\n… (background result truncated)"
            try:
                await route_bot.send_message(chat_id=chat_id, text=payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "Unsolicited Telegram delivery failed: user=%s session=%s error=%s",
                    user_id,
                    session_id,
                    type(exc).__name__,
                )
                health_reporter.record_claude_error(
                    f"Unsolicited Telegram delivery failed: {type(exc).__name__}"
                )
                return
            health_reporter.record_claude_ok()
            log_chat(user_id, session_id, "assistant", content, model=model)

        setter(deliver_unsolicited)

    def _register_agent_frame_observer(self, session: Any, *, user_id: int, chat_id: int) -> None:
        """Feed adapter-path SDK frames into the /usage recorders.

        #584 C-1 follow-up: without this seam nothing recorded the
        ResultMessage usage/cost snapshots or the RateLimitEvent windows that
        ``get_usage`` aggregates — /usage rendered every line "unavailable"
        while turns worked. ``ClaudeSession.set_sdk_frame_observer`` replays
        the raw SDK frames (turn-bearing and between-turns flows alike); this
        observer routes them into the shared recorders.

        Observation-only: token/request metering stays on the adapter seam
        (``record_claude_adapter_attempt`` / ``record_claude_adapter_result``),
        so nothing double-charges the usage meter. Runtimes without the seam
        (Codex serves /usage from its own ``get_usage`` endpoint) and
        non-Claude providers are untouched.
        """

        if getattr(self._config, "agent_provider", "claude") != "claude":
            return
        setter = getattr(session, "set_sdk_frame_observer", None)
        if not callable(setter):
            return

        def observe_sdk_frame(message: Any) -> None:
            if isinstance(message, RateLimitEvent):
                # Account-global windows, deliberately not conversation-scoped
                # (see ``_claude_rate_limit`` in project_chat).
                self._record_claude_rate_limit(message)
            elif isinstance(message, ResultMessage):
                self.record_claude_result_snapshot(user_id, chat_id, message)

        setter(observe_sdk_frame)

    async def _agent_progress_loop(self, request: _PendingRequest) -> None:
        """Keep provider-neutral turns visibly alive between runtime events."""
        try:
            while not request.future.done():
                now = asyncio.get_running_loop().time()
                if (
                    request.typing_callback is not None
                    and self._should_refresh_typing(request, now)
                    and now - request.last_typing_at >= self._typing_interval_seconds
                ):
                    request.last_typing_at = now
                    try:
                        await request.typing_callback()
                    except Exception:
                        pass
                try:
                    await self._maybe_update_heartbeat(request, now)
                except Exception as exc:
                    logger.warning(
                        "Provider-neutral heartbeat update failed: %s",
                        type(exc).__name__,
                    )
                try:
                    await asyncio.wait_for(
                        asyncio.shield(request.future),
                        timeout=self._typing_interval_seconds,
                    )
                except TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Provider-neutral progress loop failed")

    async def _apply_turn_event_transition(
        self,
        transition: TurnEventTransition,
        *,
        now: float,
        request: _PendingRequest,
        output: TurnOutputBuffer,
        streaming_handler: Optional[Any],
        deliver_pending_interim: Callable[[], Awaitable[None]],
        usage_mode: str,
    ) -> None:
        """Execute the existing ordered effects for one typed turn transition."""

        if isinstance(transition, TextDeltaTransition):
            # A new text delta after a completed message proves the prior
            # message was interim rather than final.
            await deliver_pending_interim()
            request.last_text_at = now
            output.append_delta(transition.event.text)
            if streaming_handler:
                await streaming_handler.update_if_needed(transition.event.text)
                request.last_visible_progress_at = now
            return
        if isinstance(transition, MessageCompletedTransition):
            output.complete_message(self._clean_response)
            return
        if isinstance(transition, ToolStartedTransition):
            # Tool work means the completed text should be a separate bubble
            # now, not at turn completion.
            await deliver_pending_interim()
            request.last_tool_at = now
            if transition.current_tool_label is not None:
                request.current_tool_label = transition.current_tool_label
            if streaming_handler:
                await streaming_handler.add_tool_call(
                    transition.event.tool_name,
                    dict(transition.event.arguments),
                )
                request.last_visible_progress_at = now
            return
        if isinstance(transition, ToolCompletedTransition):
            request.current_tool_label = transition.current_tool_label
            return
        if isinstance(transition, ResultTransition):
            # Terminal usage payload: the Claude adapter path meters its tokens
            # here; Codex meters via the runtime's usage-recorder seam. The
            # meter write is fsync-backed, so it runs off the loop (#1479).
            await self._run_usage_write(
                (self.record_danso_result if getattr(self._config, "agent_provider", "claude") == "danso"
                 else self.record_claude_adapter_result), transition.event, mode=usage_mode
            )
            return
        if isinstance(transition, DelegatedTaskLifecycleTransition):
            # Aggregate lifecycle state is consumed by the timeout selector and
            # health projection only. It is never rendered as assistant text.
            return
        if isinstance(transition, IgnoredTransition) and isinstance(
            transition.event, TaskProgressEvent
        ):
            # Keep the regular body-free heartbeat useful during a long native
            # task without turning checkpoint metadata into assistant text.
            transition_event = transition.event
            request.current_tool_label = (
                f"Danso task {transition_event.state} "
                f"stage {transition_event.stage} "
                f"({transition_event.requests} requests)"
            )
            return
        if isinstance(transition, (ErrorTransition, IgnoredTransition)):
            # Error payload is retained by TurnEventState for the outer response.
            # Reasoning remains private; approval/completion are already consumed.
            return

    def _build_streaming_handler(
        self,
        *,
        bot: Optional[Any],
        chat_id: int,
        user_id: int,
        streaming_sink: Optional[Any],
    ) -> Optional[Any]:
        """Pick the turn's streaming handler (#896 PR1: moved out of the turn).

        A frontend-provided sink (Matrix, tests) replaces the Telegram draft
        editor; the handler only ever calls the five-method contract. Without
        a sink, the Telegram draft editor is used only when streaming is on.
        """
        if streaming_sink is not None:
            from telegram_bot.core.streaming_sink import validate_streaming_sink

            return validate_streaming_sink(streaming_sink)
        if bot and getattr(self._config, "enable_streaming", False):
            from telegram_bot.core.streaming import StreamingMessageHandler

            return StreamingMessageHandler(bot, chat_id, user_id, settings=self._config)
        return None

    async def _acquire_turn_session(
        self,
        *,
        key: str,
        user_id: int,
        chat_id: int,
        session_id: Optional[str],
        model: Optional[str],
        effort: Optional[str],
        approval_policy: Optional[str],
        approvals_reviewer: Optional[str],
        sandbox_policy: Optional[Mapping[str, AgentJsonValue]],
        new_session: bool,
        dispatch_guard: Callable[[], bool] | None,
        notification_bot: Optional[Any],
        bot: Optional[Any],
        loop: asyncio.AbstractEventLoop,
        progress_request: Any,
        slot: "_TurnSessionSlot",
    ) -> Optional[ChatResponse]:
        """Start or resume the conversation's agent session under the guard lock.

        #896 PR1: pure move of the session-acquisition critical section out of
        ``_process_agent_message``. Session construction and the periodic
        resource guard share this short critical section. It prevents an
        idle-runtime recycle from landing between start_or_resume() and active
        registration, while turns remain parallel after admission.

        Writes ``runtime``/``session``/``turn_token`` into ``slot`` at the same
        points the inline code assigned them, and returns the refusal reply
        when admission is refused (``None`` on success). The caller adopts the
        slot in a ``finally`` so its own ``except``/``finally`` see the same
        locals as before on every exit — success, refusal, or exception.
        """
        async with self._session_guard_lock:
            # The signal callback and this check run on the same event
            # loop.  A request already past this point owns admission;
            # one waiting on the guard when drain begins must not start
            # a provider process or turn.
            if getattr(self, "_shutdown_draining", False):
                _claim_request_terminal(
                    progress_request,
                    RequestPhase.FAILED,
                    cause="bridge-draining",
                )
                return ChatResponse(
                    content=f"⏳ {_DRAIN_RESPONSE}",
                    success=False,
                    error="bridge_draining",
                    session_id=session_id,
                )
            runtime = self._require_runtime()
            slot.runtime = runtime
            cached = self._agent_session_registry.get_cached(key)
            entry = cached.entry if cached is not None else None
            session = entry.session if entry is not None else None
            slot.session = session
            if new_session or (
                entry is not None
                and (
                    (
                        session_id is not None
                        and entry.session.session_id != session_id
                    )
                    or entry.model != model
                    or entry.effort != effort
                    or entry.approval_policy != approval_policy
                    or entry.approvals_reviewer != approvals_reviewer
                    or entry.sandbox_policy != sandbox_policy
                )
            ):
                await self._drop_agent_session(key, session)
                session = None
                slot.session = None
            if session is None:
                await self._prepare_agent_session_start_locked()
                memory_environment = None
                provider = getattr(self._config, "agent_provider", "claude")
                audience = resolve_memory_audience(
                    self._config,
                    user_id=user_id,
                    chat_id=chat_id,
                    route=getattr(self, "_memory_route", "telegram"),
                )
                if audience is not None:
                    if provider == "codex":
                        memory_environment = audience.codex_environment(
                            self._config
                        )
                    elif provider == "claude":
                        memory_environment = audience.claude_environment(
                            self._config
                        )
                    elif provider == "danso":
                        memory_environment = audience.danso_environment(self._config)
                    elif provider == "piri":
                        memory_environment = audience.piri_environment(
                            self._config
                        )
                session = await runtime.start_or_resume(
                    SessionRequest(
                        working_directory=str(
                            (getattr(self._config, "danso_workspace", None) or self.project_root)
                            if provider == "danso" else self.project_root
                        ),
                        session_id=None if new_session else session_id,
                        model=model,
                        effort=effort,
                        approval_policy=approval_policy,
                        approvals_reviewer=approvals_reviewer,
                        sandbox_policy=sandbox_policy,
                        memory_environment=memory_environment,
                    )
                )
                slot.session = session
                if dispatch_guard is not None and not dispatch_guard():
                    # The session is started but not cached; the caller's
                    # finally still sees it through the slot (#896 PR1).
                    return ChatResponse(content="Recovery selection expired; use /task_recover.", success=False)
                if getattr(self, "_agent_connection_error_reported", False):
                    # #1721: the transport healed (recycle + successful
                    # session start) — clear the degraded agent state.
                    self._agent_connection_error_reported = False
                    health_reporter.record_agent_ok()
                recorder = getattr(self, "_session_started_recorder", None)
                if recorder is not None:
                    # Persist the identity before any tool can execute. A failed
                    # write must abort this turn, never orphan an uncertain journal.
                    if dispatch_guard is None:
                        await recorder(user_id, chat_id, session.session_id)
                    else:
                        await recorder(user_id, chat_id, session.session_id, dispatch_guard=dispatch_guard)
                self._agent_session_attachments += 1
                self._agent_session_registry.put_cached(
                    key,
                    AgentSessionEntry(
                        session=session,
                        model=model,
                        effort=effort,
                        approval_policy=approval_policy,
                        approvals_reviewer=approvals_reviewer,
                        sandbox_policy=sandbox_policy,
                        last_used_at=loop.time(),
                    ),
                )
            elif cached is not None:
                self._agent_session_registry.touch_cached_if_same(
                    key,
                    cached.token,
                    loop.time(),
                )

            # (Re-)register the between-turns delivery route each turn
            # so the autonomous-output path always targets the latest
            # bot reference for this conversation.
            self._register_agent_unsolicited_handler(
                session,
                user_id=user_id,
                chat_id=chat_id,
                model=model,
                route_bot=notification_bot or bot,
            )
            # Same cadence as the unsolicited route: (re-)register the
            # /usage observation seam each turn.
            self._register_agent_frame_observer(
                session, user_id=user_id, chat_id=chat_id
            )
            slot.turn_token = self._agent_session_registry.register_active(
                key,
                session,
                started_at=loop.time(),
            )
        return None

    def _make_approval_handler(
        self,
        *,
        user_id: int,
        chat_id: int,
        generation: int,
        turn_token: Any,
        progress_request: Any,
        approval_callback: Optional[AgentApprovalCallback],
        active_approval_callbacks: set[asyncio.Task[Any]],
    ) -> Callable[[ApprovalRequestEvent], Awaitable[ApprovalDecision]]:
        """Turn approval handler (#896 PR2: the inline closure, moved verbatim)."""
        async def handle_approval(
            event: ApprovalRequestEvent,
        ) -> ApprovalDecision:
            # #1045: every deny names its decision point (body-free).
            def _deny(reason: str) -> ApprovalDecision:
                _log_approval_route_deny(
                    reason=reason,
                    event=event,
                    user_id=user_id,
                    chat_id=chat_id,
                    generation=generation,
                )
                return ApprovalDecision.DENY

            if approval_callback is None:
                return _deny("no-approval-callback")
            if not self.is_agent_approval_active(user_id, chat_id, generation):
                return _deny("approval-inactive")
            # Some runtimes resolve the approval before yielding its
            # normalized event. The request is provider-admitted at
            # that boundary even though consume_agent_events has not
            # observed the event yet.
            if progress_request.lifecycle.admit():
                self._agent_session_registry.admit_if_same(turn_token)
                await self._project_request_phase(progress_request)
            approval_lease = progress_request.lifecycle.begin_approval()
            if approval_lease is None:
                return _deny("lifecycle-refused")
            callback_task = asyncio.current_task()
            if callback_task is not None:
                active_approval_callbacks.add(callback_task)
            await self._project_request_phase(progress_request)
            try:
                try:
                    decision = await approval_callback(chat_id, user_id, event, generation)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Provider-neutral approval callback failed")
                    return _deny("callback-exception")
            finally:
                if progress_request.lifecycle.end_approval(approval_lease):
                    await self._project_request_phase(progress_request)
                if callback_task is not None:
                    active_approval_callbacks.discard(callback_task)
            if decision is ApprovalDecision.ALLOW:
                if self.is_agent_approval_active(user_id, chat_id, generation):
                    return ApprovalDecision.ALLOW
                return _deny("approval-inactive-after-allow")
            return _deny("callback-deny")

        return handle_approval

    def _make_interim_deliverer(
        self,
        *,
        output: TurnOutputBuffer,
        streaming_handler: Optional[Any],
        interim_message_callback: Optional[InterimMessageCallback],
    ) -> Callable[[], Awaitable[None]]:
        """Pending-interim delivery (#896 PR2: the inline closure, moved verbatim)."""
        async def deliver_pending_interim() -> None:
            """Deliver a completed message only after more turn work appears."""
            content = output.pending_interim
            if content is None:
                return
            delivered = False
            if streaming_handler is not None:
                delivered = await streaming_handler.finalize_segment()
            elif interim_message_callback is not None:
                try:
                    await interim_message_callback(content)
                    delivered = True
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "Interim assistant message delivery failed; "
                        "retaining it for final delivery"
                    )
            output.resolve_pending_interim(delivered=delivered)

        return deliver_pending_interim

    def _make_event_applier(
        self,
        *,
        turn_token: Any,
        session: Any,
        progress_request: Any,
        output: TurnOutputBuffer,
        turn_state: TurnEventState,
        streaming_handler: Optional[Any],
        deliver_pending_interim: Callable[[], Awaitable[None]],
        usage_mode: str,
    ) -> Callable[[AgentEvent, float], Awaitable[TurnEventDirective]]:
        """Per-event transition applier (#896 PR2: the inline closure, moved verbatim)."""
        async def apply_agent_event(
            event: AgentEvent,
            now: float,
        ) -> TurnEventDirective:
            if not turn_state.admitted:
                request_admitted = progress_request.lifecycle.admit()
                if (
                    not request_admitted
                    and progress_request.lifecycle.phase
                    not in {
                        RequestPhase.WORKING,
                        RequestPhase.INPUT_REQUIRED,
                    }
                ):
                    logger.warning(
                        "Discarding runtime event after request "
                        "admission was already closed"
                    )
                    return TurnEventDirective.STOP
                turn_state.mark_admitted()
                self._agent_session_registry.admit_if_same(turn_token)
                if request_admitted:
                    await self._project_request_phase(progress_request)
            progress_request.last_event_at = now
            if turn_state.needs_attempt_recording:
                # Claude adapter-path spend boundary (#388): ClaudeRuntime
                # has no turn-attempt seam, so the first accepted event
                # meters the request. Codex meters at its own boundary.
                # Offloaded fsync-backed meter write (#1479).
                await self._run_usage_write(
                    self.record_claude_adapter_attempt, mode=usage_mode
                )
                turn_state.mark_attempt_recorded()
            # Opt-in lifecycle audit (#645): fail-open tap, never blocks
            # the turn. No-op on a default node.
            _observer = getattr(self, "_lifecycle_observer", None)
            if _observer is not None:
                _observer.observe(event, session_id=session.session_id)
            transition = turn_state.observe(event, observed_at=now)
            if isinstance(transition, DelegatedTaskLifecycleTransition):
                try:
                    health_reporter.record_delegated_task_activity(
                        id(progress_request),
                        turn_state.delegated_tasks_active,
                    )
                except Exception:
                    pass
            await self._apply_turn_event_transition(
                transition,
                now=now,
                request=progress_request,
                output=output,
                streaming_handler=streaming_handler,
                deliver_pending_interim=deliver_pending_interim,
                usage_mode=usage_mode,
            )
            if (
                turn_state.delegated_tasks_active > 0
                and output.has_text
                and not turn_state.terminal_stall_deferral_recorded
            ):
                turn_state.terminal_stall_deferral_recorded = True
                try:
                    health_reporter.record_terminal_stall_deferred_for_tasks()
                except Exception:
                    pass
            return TurnEventDirective.CONTINUE

        return apply_agent_event

    def _make_turn_interrupter(
        self,
        *,
        user_id: int,
        chat_id: int,
        session: Any,
        progress_request: Any,
        turn_state: TurnEventState,
        callbacks: "_TurnCallbacks",
        active_approval_callbacks: set[asyncio.Task[Any]],
    ) -> Callable[[TurnStreamOutcome], Awaitable[None]]:
        """Stall/interrupt path (#896 PR2: the inline closure, moved verbatim).

        The inline closure wrote ``nonlocal approval_stall_won``; it now writes
        ``callbacks.approval_stall_won``. Its own ``callbacks = tuple(...)``
        local was renamed ``pending`` so it cannot shadow that object.
        """
        async def interrupt_turn(outcome: TurnStreamOutcome) -> None:
            if turn_state.approval_pending:
                if outcome is TurnStreamOutcome.APPROVAL_STALL:
                    callbacks.approval_stall_won = _claim_request_terminal(
                        progress_request,
                        RequestPhase.TIMEOUT,
                        cause="approval-stall",
                    )
                    if not callbacks.approval_stall_won:
                        # A concurrent /stop (or another terminal owner)
                        # already won. Let that path own the session/UI
                        # side effects and keep this request silent.
                        raise asyncio.CancelledError
                self.invalidate_agent_approvals(user_id, chat_id)
                pending = tuple(active_approval_callbacks)
                for callback in pending:
                    callback.cancel()
                if pending:
                    await asyncio.gather(
                        *(asyncio.shield(callback) for callback in pending),
                        return_exceptions=True,
                    )
            await self._interrupt_agent_session(session)

        return interrupt_turn

    def _make_turn_callbacks(
        self,
        *,
        user_id: int,
        chat_id: int,
        generation: int,
        turn_token: Any,
        session: Any,
        progress_request: Any,
        output: TurnOutputBuffer,
        turn_state: TurnEventState,
        streaming_handler: Optional[Any],
        approval_callback: Optional[AgentApprovalCallback],
        interim_message_callback: Optional[InterimMessageCallback],
        usage_mode: str,
    ) -> "_TurnCallbacks":
        """Build the four per-turn callbacks consume_turn_stream drives (#896 PR2).

        Pure move of the closures that used to be defined inline in
        ``_process_agent_message``: each still closes over the same names, now
        bound once as factory parameters after the session and the active-turn
        token exist. ``active_approval_callbacks`` is shared between the
        approval handler and the interrupter exactly as before, and the one
        ``nonlocal`` (``approval_stall_won``) became an attribute on the
        returned object so the caller can read it once the stream has ended.
        One factory per closure keeps each under the CC 15 gate.
        """
        callbacks = _TurnCallbacks()
        active_approval_callbacks: set[asyncio.Task[Any]] = set()
        callbacks.handle_approval = self._make_approval_handler(
            user_id=user_id,
            chat_id=chat_id,
            generation=generation,
            turn_token=turn_token,
            progress_request=progress_request,
            approval_callback=approval_callback,
            active_approval_callbacks=active_approval_callbacks,
        )
        callbacks.deliver_pending_interim = self._make_interim_deliverer(
            output=output,
            streaming_handler=streaming_handler,
            interim_message_callback=interim_message_callback,
        )
        callbacks.apply_agent_event = self._make_event_applier(
            turn_token=turn_token,
            session=session,
            progress_request=progress_request,
            output=output,
            turn_state=turn_state,
            streaming_handler=streaming_handler,
            deliver_pending_interim=callbacks.deliver_pending_interim,
            usage_mode=usage_mode,
        )
        callbacks.interrupt_turn = self._make_turn_interrupter(
            user_id=user_id,
            chat_id=chat_id,
            session=session,
            progress_request=progress_request,
            turn_state=turn_state,
            callbacks=callbacks,
            active_approval_callbacks=active_approval_callbacks,
        )
        return callbacks

    async def _run_turn_stream(
        self,
        *,
        session: Any,
        turn_message: str,
        callbacks: "_TurnCallbacks",
        turn_state: TurnEventState,
        output: TurnOutputBuffer,
        abort_stalled_turn: Any,
        admission_grace: float,
        approval_grace: float,
        stall_grace: float,
        delegated_stall_grace: float,
    ) -> TurnStreamOutcome:
        """Send the turn and consume its stream under the whole-turn timeout (#896 PR2).

        Deliberately no ``try`` here: ``asyncio.TimeoutError`` from
        ``wait_for`` and every other exception keep propagating to the
        caller's ``except`` clauses in their original order.
        """
        return await asyncio.wait_for(
            consume_turn_stream(
                session.send_turn(
                    turn_message,
                    approval_handler=callbacks.handle_approval,
                ).__aiter__(),
                state=turn_state,
                has_text=lambda: output.has_text,
                on_event=callbacks.apply_agent_event,
                interrupt=callbacks.interrupt_turn,
                abort_stalled_turn=abort_stalled_turn,
                admission_timeout_seconds=admission_grace,
                approval_stall_seconds=approval_grace,
                terminal_stall_seconds=stall_grace,
                delegated_task_stall_seconds=delegated_stall_grace,
                interrupt_timeout_seconds=self._agent_interrupt_timeout_seconds,
            ),
            timeout=self._process_timeout_seconds,
        )

    async def _resolve_turn_outcome(
        self,
        *,
        turn_outcome: TurnStreamOutcome,
        key: str,
        session: Any,
        model: Optional[str],
        loop: asyncio.AbstractEventLoop,
        progress_request: Any,
        streaming_handler: Any,
        output: TurnOutputBuffer,
        turn_state: TurnEventState,
        callbacks: "_TurnCallbacks",
        user_id: int,
        chat_id: int,
        admission_grace: float,
        approval_grace: float,
        stall_grace: float,
        delegated_stall_grace: float,
    ) -> Optional[ChatResponse]:
        """Turn a stalled ``TurnStreamOutcome`` into its user-facing reply (#896 PR3).

        Pure move of the four stall branches (admission timeout, approval
        stall, terminal stall, delegated-task stall). Returns ``None`` for
        ``COMPLETED`` so the caller continues with ``_finish_completed_turn``.
        The approval-stall defence still *raises* ``CancelledError``: it is
        awaited inside the caller's ``try``, so the caller's
        ``except asyncio.CancelledError`` receives it exactly as before.
        Diagnostics are read before ``_drop_agent_session`` in every branch
        (#846) — keep the statement order.
        """
        if turn_outcome is TurnStreamOutcome.ADMISSION_TIMEOUT:
            # Collected before the session is dropped: closing the
            # client tears down the SDK transport and takes the exit
            # code with it.
            diagnostics = _admission_diagnostics(
                session,
                provider=getattr(self._config, "agent_provider", "claude"),
                model=model,
                elapsed=_elapsed_since(loop, progress_request),
                grace=admission_grace,
            )
            terminal_won = _claim_request_terminal(
                progress_request,
                RequestPhase.TIMEOUT,
                cause="admission-timeout",
            )
            if terminal_won:
                await self._drop_agent_session(key, session)
            logger.warning(
                "Turn admission timed out for user %s chat %s before the "
                "runtime produced its first event "
                "(provider=%s model=%s endpoint=%s elapsed=%ss grace=%gs "
                "exit_code=%s stderr_class=%s stderr_lines=%s)",
                user_id,
                chat_id,
                diagnostics["provider"],
                diagnostics["model"],
                diagnostics["endpoint"],
                diagnostics["elapsed"],
                admission_grace,
                diagnostics["exit_code"],
                diagnostics["stderr_class"],
                diagnostics["stderr_lines"],
            )
            try:
                health_reporter.record_stalled_request()
            except Exception:
                pass
            message = f"Agent turn did not start within {admission_grace:g}s"
            return ChatResponse(
                content=f"⏰ {message}. Please retry your request.",
                success=False,
                error=message,
                failure_class=(
                    "admission-timeout/"
                    f"{diagnostics['stderr_class'] or 'silent'}"
                ),
                session_id=session.session_id,
            )

        if turn_outcome is TurnStreamOutcome.APPROVAL_STALL:
            if not callbacks.approval_stall_won:
                # Defensive: the approval timeout claims lifecycle
                # authority in interrupt_turn before any abort effects.
                raise asyncio.CancelledError
            await self._drop_agent_session(key, session)
            await self._cancel_agent_streaming(
                streaming_handler,
                context="handling an approval-stall timeout",
            )
            # #1555: name the request that was still outstanding —
            # body-free tool name + target kind, never the arguments —
            # so the operator can tell a Bash lane from a Write lane
            # without correlating bridge log lines.
            pending_label = turn_state.approval_pending_label
            pending_suffix = f" (pending: {pending_label})" if pending_label else ""
            logger.warning(
                "Approval stall released agent turn for user %s chat %s "
                "after %.1fs without a decision%s",
                user_id,
                chat_id,
                approval_grace,
                pending_suffix,
            )
            try:
                health_reporter.record_stalled_request()
            except Exception:
                pass
            message = (
                f"Approval was not resolved within {approval_grace:g}s{pending_suffix}"
            )
            return ChatResponse(
                content=(
                    f"⏰ {message}. The stalled turn was stopped; "
                    "please retry your request."
                ),
                success=False,
                error=message,
                session_id=session.session_id,
            )

        if turn_outcome is TurnStreamOutcome.TERMINAL_STALL:
            # Diagnostics are read before the session is dropped —
            # closing the client clears the SDK transport and the exit
            # code with it (same ordering as ADMISSION_TIMEOUT, #846).
            stall_diagnostics = _admission_diagnostics(
                session,
                provider=getattr(self._config, "agent_provider", "claude"),
                model=model,
                elapsed=_elapsed_since(loop, progress_request),
                grace=stall_grace,
            )
            stall_ages = _stall_ages(loop, progress_request)
            terminal_won = _claim_request_terminal(
                progress_request,
                RequestPhase.INTERRUPTED,
                cause="terminal-stall",
            )
            if terminal_won:
                await self._drop_agent_session(key, session)
            final_streamed = False
            if streaming_handler:
                final_streamed = await streaming_handler.finalize_all()
            logger.warning(
                "Terminal-event stall released agent turn for user %s chat %s "
                "after silence following answer text "
                "(provider=%s model=%s endpoint=%s elapsed=%ss grace=%gs "
                "silence=%ss last_text_age=%ss last_tool_age=%ss "
                "exit_code=%s stderr_class=%s stderr_lines=%s)",
                user_id,
                chat_id,
                stall_diagnostics["provider"],
                stall_diagnostics["model"],
                stall_diagnostics["endpoint"],
                stall_diagnostics["elapsed"],
                stall_grace,
                stall_ages["silence"],
                stall_ages["last_text_age"],
                stall_ages["last_tool_age"],
                stall_diagnostics["exit_code"],
                stall_diagnostics["stderr_class"],
                stall_diagnostics["stderr_lines"],
            )
            try:
                health_reporter.record_stalled_request()
            except Exception:
                pass
            content = output.render(self._clean_response)
            streamed = final_streamed
            if not content and output.interim_delivered:
                streamed = True
            content = content or "(No response)"
            message = "Agent stopped before terminal completion"
            return ChatResponse(
                content=f"{content}\n\n{TERMINAL_STALL_NOTICE}",
                success=False,
                error=message,
                session_id=session.session_id,
                streamed=streamed,
            )

        if turn_outcome is TurnStreamOutcome.DELEGATED_TASK_STALL:
            terminal_won = _claim_request_terminal(
                progress_request,
                RequestPhase.INTERRUPTED,
                cause="delegated-task-stall",
            )
            if terminal_won:
                await self._drop_agent_session(key, session)
            await self._cancel_agent_streaming(
                streaming_handler,
                context="handling a delegated-task stall",
            )
            logger.warning(
                "Delegated-task stall released agent turn for user %s chat %s "
                "after oldest task exceeded %gs (active_count=%d)",
                user_id,
                chat_id,
                delegated_stall_grace,
                turn_state.delegated_tasks_active,
            )
            try:
                health_reporter.record_delegated_task_stall()
            except Exception:
                pass
            content = output.render(self._clean_response) or "(No response)"
            message = "Delegated work exceeded its maximum runtime"
            return ChatResponse(
                content=(
                    f"{content}\n\n⏰ {message}; the turn was stopped "
                    "and the conversation queue was released."
                ),
                success=False,
                error=message,
                session_id=session.session_id,
            )
        return None

    async def _finish_completed_turn(
        self,
        *,
        key: str,
        session: Any,
        runtime: Any,
        progress_request: Any,
        streaming_handler: Any,
        output: TurnOutputBuffer,
        turn_state: TurnEventState,
        user_id: int,
        chat_id: int,
    ) -> ChatResponse:
        """Build the reply for a ``COMPLETED`` stream (#896 PR3).

        Pure move: finalize streaming, surface a provider-terminal error as
        a typed failure, and classify an empty completion as recovered /
        coalesced / retryable (#775, #1128) before claiming
        ``RequestPhase.COMPLETED``.
        """
        final_streamed = False
        if streaming_handler:
            final_streamed = await streaming_handler.finalize_all()
        content = output.render(self._clean_response)
        streamed = final_streamed
        if not content and output.interim_delivered:
            streamed = True
        terminal_error = turn_state.terminal_error
        if terminal_error is not None:
            # The user sees "❌ Processing failed: ..." but nothing
            # reached the log, so a provider-terminal failure left no
            # server-side trace at all. Measured on dungae
            # (2026-08-04): a crush turn died with `tool_use` and
            # bot.log / error_*.log were both silent, which cost a
            # full diagnosis round. Log the normalized fields —
            # ErrorEvent carries `code` and `retryable`, neither of
            # which the user-facing string shows. Body-free: the
            # message is provider-normalized, no chat content.
            logger.error(
                "Turn failed: provider=%s code=%s retryable=%s message=%s",
                getattr(self._config, "agent_provider", "claude"),
                terminal_error.code,
                terminal_error.retryable,
                terminal_error.message,
            )
            terminal_won = _claim_request_terminal(
                progress_request,
                RequestPhase.FAILED,
                cause="runtime-error",
            )
            if terminal_won:
                await self._drop_agent_session(key, session)
            if terminal_error.code == "danso_task_paused":
                return ChatResponse(
                    content=f"⏸ {terminal_error.message}",
                    success=False,
                    error=terminal_error.message,
                    session_id=session.session_id,
                    failure_class="danso_task_paused",
                    failure_code=terminal_error.code,
                )
            return ChatResponse(
                content=f"❌ Processing failed: {terminal_error.message}",
                success=False,
                error=terminal_error.message,
                session_id=session.session_id,
                # The error itself was not part of the assistant draft.
                # Always deliver it even when interim text was streamed.
                streamed=False,
                failure_code=terminal_error.code,
            )
        if not content and not streamed:
            # #775: a successful terminal result without user-visible
            # text must not masquerade as a "(No response)" success.
            # When the provider's terminal payload preserved the final
            # answer (no visible event carried it), deliver that text
            # once; otherwise classify the turn as a typed failure.
            recovered = self._clean_response(turn_state.terminal_result_text or "")
            provider_label = type(runtime).__name__
            if recovered:
                logger.warning(
                    "Empty normal completion for user %s chat %s recovered "
                    "from the terminal result payload: provider=%s "
                    "cause=no-visible-text-events",
                    user_id,
                    chat_id,
                    provider_label,
                )
                try:
                    health_reporter.record_empty_completion(recovered=True)
                except Exception:
                    pass
                content = recovered
            else:
                # #1128: two messages sent within about a second reach
                # the provider as a single turn, so exactly one request
                # carries the answer and the rest end with no visible
                # text. A follower queued behind this turn is the only
                # in-process evidence of that; the queue lives inside
                # the CLI, so the alternative would be parsing its
                # transcript and coupling the bridge to that format.
                followers = self._conversation_followers(key)
                coalesced = followers > 0
                cause = "coalesced-turn" if coalesced else "empty-completion"
                _claim_request_terminal(
                    progress_request,
                    RequestPhase.FAILED,
                    cause=cause,
                )
                logger.warning(
                    "Empty normal completion for user %s chat %s: "
                    "provider=%s finished successfully without "
                    "user-visible text (session kept) cause=%s "
                    "followers=%s",
                    user_id,
                    chat_id,
                    provider_label,
                    cause,
                    followers,
                )
                try:
                    health_reporter.record_empty_completion(
                        recovered=False, coalesced=coalesced
                    )
                except Exception:
                    pass
                if coalesced:
                    # Not a failure: the turn ran, and its answer is
                    # already on its way under the follower. Telling
                    # the user to retry would queue yet another
                    # message and reproduce the same split.
                    message = "Handled together with your other message"
                    return ChatResponse(
                        content=f"⏳ {message}; the answer arrives with that one.",
                        success=False,
                        error="coalesced_turn",
                        session_id=session.session_id,
                        # Typed for diagnostics; deliberately NOT in
                        # the retry set — the follower carries the
                        # answer and a resend would duplicate the
                        # question (#1128).
                        failure_class="coalesced-turn",
                    )
                message = "Agent finished without a visible answer"
                return ChatResponse(
                    content=f"❌ {message}. Please retry your request.",
                    success=False,
                    error=message,
                    session_id=session.session_id,
                    # Lets process_message's bounded retry take one
                    # more shot instead of pushing the retry onto
                    # the user (gwakga 2026-08-18 14:44).
                    failure_class=_EMPTY_COMPLETION_MARKER,
                )
        _claim_request_terminal(
            progress_request,
            RequestPhase.COMPLETED,
            cause="normal-completion",
        )
        return ChatResponse(
            content=content,
            success=True,
            session_id=session.session_id,
            streamed=streamed,
        )

    async def _process_agent_message(  # noqa: C901 -- #348 baseline hotspot
        self,
        *,
        user_message: str,
        user_id: int,
        chat_id: int,
        session_id: Optional[str],
        model: Optional[str],
        effort: Optional[str],
        approval_policy: Optional[str],
        approvals_reviewer: Optional[str],
        sandbox_policy: Optional[Mapping[str, AgentJsonValue]],
        new_session: bool,
        approval_callback: Optional[AgentApprovalCallback],
        typing_callback: Optional[TypingCallback],
        status_callback: Optional[StatusCallback],
        bot: Optional[Any],
        interim_message_callback: Optional[InterimMessageCallback],
        notification_bot: Optional[Any] = None,
        usage_mode: str = MODE_INTERACTIVE,
        admission_timeout_override: Optional[float] = None,
        skill_advice_allowed: bool = True,
        resume_task: bool = False,
        dispatch_guard: Callable[[], bool] | None = None,
        streaming_sink: Optional[Any] = None,
    ) -> ChatResponse:
        """Run one provider-neutral turn without changing the Claude SDK path.

        ``admission_timeout_override`` exists for the retry in
        ``process_message`` and nothing else. The retry must not spend the full
        admission grace a second time — that is how a 300s deadline became a
        600s wait on gongmyoung — so it re-runs on a short budget instead.
        """
        key = self._stream_key(user_id, chat_id)
        if model is None and getattr(self._config, "agent_provider", "claude") == "crush":
            # crush has no built-in safe default: without an explicit model it
            # activates its bundled provider default and can route the turn to
            # an unintended backend (canary4 401, #926). Pin the configured
            # default (CCC_CRUSH_MODEL) unless the turn chose a model itself.
            model = getattr(self._config, "crush_model", None) or None
        streaming_handler = self._build_streaming_handler(
            bot=bot, chat_id=chat_id, user_id=user_id, streaming_sink=streaming_sink
        )

        async with self._conversation_turn(user_id, chat_id):
            if dispatch_guard is not None and not dispatch_guard():
                return ChatResponse(content="Recovery selection expired; use /task_recover.", success=False)
            loop = asyncio.get_running_loop()
            # Next-turn reclaim of route-bound async completions (#646 slice
            # 3): body-free notice under the conversation lock, before any
            # turn output. Fail-open; interactive turns only.
            await self._maybe_reclaim_async_completions(user_id, chat_id, usage_mode)
            progress_coordinator = RequestProgressCoordinator(
                ledger_create=self._ledger_create,
                progress_loop=self._agent_progress_loop,
                cleanup_heartbeat=self._cleanup_heartbeat,
                append_duration_log=self._append_duration_log,
                ledger_finish=self._ledger_finish,
            )
            progress_handle = await progress_coordinator.start(
                user_id=user_id,
                chat_id=chat_id,
                model=model,
                requested_session_id=session_id,
                typing_callback=typing_callback,
                status_callback=status_callback,
                streaming_handler=streaming_handler,
                usage_mode=usage_mode,
            )
            progress_request = progress_handle.request
            session = None
            turn_token: ActiveToken | None = None
            resume_authorized = False
            followup_authorized = False
            try:
                # Session construction and the periodic resource guard share
                # a short critical section inside _acquire_turn_session (#896
                # PR1). The slot is adopted in a finally so the except/finally
                # clauses below see the same ``session``/``turn_token`` the
                # inline code left on every exit, including an exception
                # between start_or_resume() and registration.
                slot = _TurnSessionSlot()
                try:
                    refusal = await self._acquire_turn_session(
                        key=key,
                        user_id=user_id,
                        chat_id=chat_id,
                        session_id=session_id,
                        model=model,
                        effort=effort,
                        approval_policy=approval_policy,
                        approvals_reviewer=approvals_reviewer,
                        sandbox_policy=sandbox_policy,
                        new_session=new_session,
                        dispatch_guard=dispatch_guard,
                        notification_bot=notification_bot,
                        bot=bot,
                        loop=loop,
                        progress_request=progress_request,
                        slot=slot,
                    )
                finally:
                    session = slot.session
                    turn_token = slot.turn_token
                if refusal is not None:
                    return refusal
                runtime = slot.runtime
                # #740: publish which conversation this turn serves so the
                # agent-side external-wait CLI can bind CI registrations to
                # the correct route (cleared in the turn finally below). This
                # is an fsync-backed file write, so it runs in a worker thread
                # and outside the session guard lock (#1479): the lock only
                # protects registry/runtime state against the resource guard,
                # nothing under it reads the route file, the conversation lock
                # already serializes same-conversation turns, and the route is
                # still published before send_turn() hands control to the agent.
                await _await_offloaded_write(
                    publish_active_turn,
                    self._external_wait_home(),
                    user_id=user_id,
                    chat_id=chat_id,
                    session_id=session.session_id,
                )
                generation = turn_token.generation
                output = TurnOutputBuffer()
                turn_state = TurnEventState()
                callbacks = self._make_turn_callbacks(
                    user_id=user_id,
                    chat_id=chat_id,
                    generation=generation,
                    turn_token=turn_token,
                    session=session,
                    progress_request=progress_request,
                    output=output,
                    turn_state=turn_state,
                    streaming_handler=streaming_handler,
                    approval_callback=approval_callback,
                    interim_message_callback=interim_message_callback,
                    usage_mode=usage_mode,
                )
                stall_grace = float(getattr(self._config, "terminal_stall_seconds", 0.0) or 0.0)
                delegated_stall_grace = float(
                    getattr(self._config, "delegated_task_stall_seconds", 7200.0)
                )
                admission_grace = float(
                    admission_timeout_override
                    if admission_timeout_override is not None
                    else (getattr(self._config, "turn_admission_timeout_seconds", 0.0) or 0.0)
                )
                if getattr(self._config, "agent_provider", "claude") == "danso":
                    admission_grace = 0.0  # Tool progress may start late; subprocess owns the finite deadline.

                approval_grace = float(
                    getattr(self._config, "approval_stall_seconds", 0.0) or 0.0
                )

                abort_stalled_turn = getattr(session, "abort_stalled_turn", None)
                if not callable(abort_stalled_turn):
                    abort_stalled_turn = None
                if dispatch_guard is not None:
                    setter = getattr(session, "set_dispatch_guard", None)
                    if not callable(setter) or not dispatch_guard():
                        return ChatResponse(content="Recovery selection expired or unsupported; use /task_recover.", success=False)
                    setter(dispatch_guard)
                if resume_task:
                    authorize_resume = getattr(session, "authorize_task_resume", None)
                    if not callable(authorize_resume):
                        return ChatResponse(
                            content="❌ Explicit Danso long-task resume is unavailable for this conversation.",
                            success=False,
                            error="danso_task_resume_unavailable",
                            session_id=session.session_id,
                        )
                    authorize_resume()
                    resume_authorized = True
                if (not resume_task and usage_mode == MODE_INTERACTIVE
                        and getattr(self._config, "agent_provider", None) == "danso"):
                    authorize_followup = getattr(session, "authorize_task_followup", None)
                    if callable(authorize_followup):
                        authorize_followup()
                        followup_authorized = True
                turn_message = await advise_turn(
                    user_message, settings=self._config, user_id=user_id, chat_id=chat_id,
                    interactive=(skill_advice_allowed and usage_mode == MODE_INTERACTIVE and not resume_task
                                 and dispatch_guard is None
                                 and admission_timeout_override is None),
                )
                turn_outcome = await self._run_turn_stream(
                    session=session,
                    turn_message=turn_message,
                    callbacks=callbacks,
                    turn_state=turn_state,
                    output=output,
                    abort_stalled_turn=abort_stalled_turn,
                    admission_grace=admission_grace,
                    approval_grace=approval_grace,
                    stall_grace=stall_grace,
                    delegated_stall_grace=delegated_stall_grace,
                )

                stall_response = await self._resolve_turn_outcome(
                    turn_outcome=turn_outcome,
                    key=key,
                    session=session,
                    model=model,
                    loop=loop,
                    progress_request=progress_request,
                    streaming_handler=streaming_handler,
                    output=output,
                    turn_state=turn_state,
                    callbacks=callbacks,
                    user_id=user_id,
                    chat_id=chat_id,
                    admission_grace=admission_grace,
                    approval_grace=approval_grace,
                    stall_grace=stall_grace,
                    delegated_stall_grace=delegated_stall_grace,
                )
                if stall_response is not None:
                    return stall_response
                return await self._finish_completed_turn(
                    key=key,
                    session=session,
                    runtime=runtime,
                    progress_request=progress_request,
                    streaming_handler=streaming_handler,
                    output=output,
                    turn_state=turn_state,
                    user_id=user_id,
                    chat_id=chat_id,
                )
            except TimeoutError:
                terminal_won = _claim_request_terminal(
                    progress_request,
                    RequestPhase.TIMEOUT,
                    cause="process-timeout",
                )
                if terminal_won and session is not None:
                    # Interrupt BEFORE dropping: _drop_agent_session closes the
                    # session, and a closed Claude session ignores interrupt —
                    # the stall paths below already use this order.
                    await self._interrupt_agent_session(session)
                    await self._drop_agent_session(key, session)
                if terminal_won:
                    await self._cancel_agent_streaming(
                        streaming_handler, context="handling an agent timeout"
                    )
                message = f"Timed out after {self._process_timeout_seconds}s"
                return ChatResponse(
                    content=f"⏰ {message}. Please retry or simplify your request.",
                    success=False,
                    error=message,
                    session_id=session.session_id if session is not None else session_id,
                )
            except asyncio.CancelledError:
                terminal_won = _claim_request_terminal(
                    progress_request,
                    RequestPhase.CANCELED,
                    cause="request-canceled",
                )
                if terminal_won and session is not None:
                    await self._interrupt_agent_session(session)
                if terminal_won:
                    await self._cancel_agent_streaming(
                        streaming_handler, context="propagating task cancellation"
                    )
                raise
            except Exception as exc:
                terminal_won = _claim_request_terminal(
                    progress_request,
                    RequestPhase.FAILED,
                    cause="runtime-exception",
                )
                if terminal_won and session is not None:
                    await self._drop_agent_session(key, session)
                if terminal_won:
                    await self._cancel_agent_streaming(
                        streaming_handler, context="returning an agent error"
                    )
                message = str(exc) or "Agent runtime failed"
                if isinstance(exc, CodexConnectionClosedError):
                    # #1721: a dead or poisoned app-server transport must show
                    # up in /status instead of "Codex: healthy" (the liveness
                    # probe only sees the process, which may still be running).
                    health_reporter.record_agent_error(
                        f"Codex app-server connection failed: {message}"
                    )
                    self._agent_connection_error_reported = True
                return ChatResponse(
                    content=f"❌ Error: {message}",
                    success=False,
                    error=message,
                    session_id=session.session_id if session is not None else session_id,
                )
            finally:
                if followup_authorized and session is not None:
                    session.clear_task_followup_authorization()
                if resume_authorized:
                    clear_resume = getattr(session, "clear_task_resume_authorization", None)
                    if callable(clear_resume):
                        clear_resume()
                try:
                    health_reporter.record_delegated_task_activity(
                        id(progress_request),
                        0,
                    )
                except Exception:
                    pass
                try:
                    await _finalize_request_progress(
                        coordinator=progress_coordinator,
                        handle=progress_handle,
                        session=session,
                        requested_session_id=session_id,
                    )
                finally:
                    try:
                        # Offloaded fsync write (#1479); a cancellation landing
                        # here is honoured only after the clear has landed so
                        # the route can never outlive the turn.
                        await _await_offloaded_write(
                            clear_active_turn,
                            self._external_wait_home(),
                            user_id=user_id,
                            chat_id=chat_id,
                            session_id=session.session_id if session is not None else None,
                        )
                    finally:
                        if turn_token is not None:
                            self._agent_session_registry.deactivate_if_same(
                                turn_token,
                                touch_at=loop.time(),
                            )
