"""Message submission/retry mixin for ProjectChatHandler."""

# mypy: disable-error-code="attr-defined"

import asyncio
import logging
import re
from collections.abc import Mapping
from typing import Any, Optional

from claude_agent_sdk import RateLimitEvent, ResultMessage

from telegram_bot.core.agent_runtime import (
    ApprovalDecision,
    ApprovalRequestEvent,
    CompletionEvent,
    ErrorEvent,
    JsonValue as AgentJsonValue,
    MessageCompletedEvent,
    ReasoningDeltaEvent,
    ResultEvent,
    SessionRequest,
    TextDeltaEvent,
    ToolCompletedEvent,
    ToolStartedEvent,
)
from telegram_bot.core.heartbeat import tool_label
from telegram_bot.core.memory_audience import resolve_memory_audience
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
from telegram_bot.core.usage_meter import MODE_INTERACTIVE
from telegram_bot.core.task_ledger import (
    CANCELED as TASK_CANCELED,
    COMPLETED as TASK_COMPLETED,
    FAILED as TASK_FAILED,
    INPUT_REQUIRED as TASK_INPUT_REQUIRED,
    TIMEOUT as TASK_TIMEOUT,
    WAITING_FOR_TURN as TASK_WAITING_FOR_TURN,
    WORKING as TASK_WORKING,
)
from telegram_bot.core.sdk_text import TERMINAL_STALL_NOTICE
from telegram_bot.utils.chat_logger import log_chat
from telegram_bot.utils.health import health_reporter

logger = logging.getLogger(__name__)


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


class ProjectChatProcessMixin:
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
    ) -> ChatResponse:
        del message_id
        # The legacy permission seam (perm: buttons) belonged to the removed
        # direct SDK path; the runtime path's approval boundary is
        # approval_callback. Accepted and ignored for caller compatibility.
        del permission_callback
        self._require_runtime()
        if getattr(self._config, "agent_provider", "claude") == "claude":
            # Claude adapter path (#584): the bot layer's approval/sandbox
            # knobs are Codex app-server policies (bot_access._codex_*) that
            # ClaudeRuntime rejects fail-closed. On this path the approval
            # boundary is the SDK can_use_tool -> approval_callback seam, so
            # drop the untranslatable Codex-only knobs instead of forwarding
            # them.
            approval_policy = None
            approvals_reviewer = None
            sandbox_policy = None
        if sensitive_log_event is not None:
            _log_user_input(
                user_message=user_message,
                user_id=user_id,
                session_id=session_id,
                model=model,
                sensitive_log_event=sensitive_log_event,
            )
        return await self._process_agent_message(
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
        )

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

    def _register_agent_frame_observer(
        self, session: Any, *, user_id: int, chat_id: int
    ) -> None:
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
    ) -> ChatResponse:
        """Run one provider-neutral turn without changing the Claude SDK path."""
        key = self._stream_key(user_id, chat_id)
        streaming_handler = None
        if bot and getattr(self._config, "enable_streaming", False):
            from telegram_bot.core.streaming import StreamingMessageHandler

            streaming_handler = StreamingMessageHandler(bot, chat_id, user_id, settings=self._config)

        async with self._get_conversation_lock(user_id, chat_id):
            loop = asyncio.get_running_loop()
            progress_future: asyncio.Future[None] = loop.create_future()
            progress_request = _PendingRequest(
                user_id=user_id,
                chat_id=chat_id,
                model=model,
                requested_session_id=session_id,
                permission_callback=None,
                typing_callback=typing_callback,
                future=progress_future,
                status_callback=status_callback,
                streaming_handler=streaming_handler,
            )
            progress_request.usage_mode = usage_mode
            progress_request.started_at = loop.time()
            progress_request.task_id = self._ledger_create(user_id, chat_id)
            ledger = self._task_ledger
            if ledger and progress_request.task_id:
                ledger.set_state(progress_request.task_id, TASK_WAITING_FOR_TURN)
            progress_task = asyncio.create_task(
                self._agent_progress_loop(progress_request),
                name=f"agent-progress-{user_id}-{chat_id}",
            )
            progress_terminal_state = TASK_FAILED
            generation = self._next_agent_generation(key)
            self._agent_active_generations[key] = generation
            entry = self._agent_sessions.get(key)
            session = entry.session if entry is not None else None
            if new_session or (
                entry is not None
                and (
                    (session_id is not None and entry.session.session_id != session_id)
                    or entry.model != model
                    or entry.effort != effort
                    or entry.approval_policy != approval_policy
                    or entry.approvals_reviewer != approvals_reviewer
                    or entry.sandbox_policy != sandbox_policy
                )
            ):
                self._agent_sessions.pop(key, None)
                session = None
            try:
                if session is None:
                    memory_environment = None
                    if getattr(self._config, "agent_provider", "claude") == "codex":
                        audience = resolve_memory_audience(
                            self._config,
                            user_id=user_id,
                            chat_id=chat_id,
                        )
                        if audience is not None:
                            memory_environment = audience.codex_environment(self._config)
                    session = await self._agent_runtime.start_or_resume(
                        SessionRequest(
                            working_directory=str(self.project_root),
                            session_id=None if new_session else session_id,
                            model=model,
                            effort=effort,
                            approval_policy=approval_policy,
                            approvals_reviewer=approvals_reviewer,
                            sandbox_policy=sandbox_policy,
                            memory_environment=memory_environment,
                        )
                    )
                    self._agent_sessions[key] = AgentSessionEntry(
                        session=session,
                        model=model,
                        effort=effort,
                        approval_policy=approval_policy,
                        approvals_reviewer=approvals_reviewer,
                        sandbox_policy=sandbox_policy,
                    )

                # (Re-)register the between-turns delivery route each turn so
                # the autonomous-output path always targets the latest bot
                # reference for this (user_id, chat_id) conversation.
                self._register_agent_unsolicited_handler(
                    session,
                    user_id=user_id,
                    chat_id=chat_id,
                    model=model,
                    route_bot=notification_bot or bot,
                )
                # Same cadence as the unsolicited route: (re-)register the
                # /usage observation seam each turn for this conversation.
                self._register_agent_frame_observer(
                    session, user_id=user_id, chat_id=chat_id
                )
                self._agent_active_sessions[key] = session
                self._agent_started_at[key] = asyncio.get_running_loop().time()
                self._agent_waiting_for_turn.add(key)
                text_parts: list[str] = []
                response_parts: list[str] = []
                current_message_parts: list[str] = []
                pending_completed_message: str | None = None
                interim_delivered = False
                terminal_error: ErrorEvent | None = None

                async def handle_approval(event: ApprovalRequestEvent) -> ApprovalDecision:
                    if approval_callback is None:
                        return ApprovalDecision.DENY
                    if not self.is_agent_approval_active(user_id, chat_id, generation):
                        return ApprovalDecision.DENY
                    progress_request.awaiting_permission = True
                    ledger = self._task_ledger
                    if ledger and progress_request.task_id:
                        ledger.set_state(progress_request.task_id, TASK_INPUT_REQUIRED)
                    try:
                        try:
                            decision = await approval_callback(
                                chat_id, user_id, event, generation
                            )
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            logger.exception("Provider-neutral approval callback failed")
                            return ApprovalDecision.DENY
                    finally:
                        progress_request.awaiting_permission = False
                        if ledger and progress_request.task_id:
                            ledger.set_state(progress_request.task_id, TASK_WORKING)
                    if (
                        decision is ApprovalDecision.ALLOW
                        and self.is_agent_approval_active(user_id, chat_id, generation)
                    ):
                        return ApprovalDecision.ALLOW
                    return ApprovalDecision.DENY

                stall_grace = float(
                    getattr(self._config, "terminal_stall_seconds", 0.0) or 0.0
                )
                admission_grace = float(
                    getattr(
                        self._config, "turn_admission_timeout_seconds", 0.0
                    )
                    or 0.0
                )
                stalled = False
                admission_stalled = False
                attempt_recorded = False

                async def deliver_pending_interim() -> None:
                    """Deliver a completed message only after more turn work appears."""
                    nonlocal pending_completed_message, interim_delivered
                    content = pending_completed_message
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
                    if delivered:
                        interim_delivered = True
                    else:
                        response_parts.append(content)
                    pending_completed_message = None

                def response_content() -> str:
                    parts = list(response_parts)
                    if pending_completed_message is not None:
                        parts.append(pending_completed_message)
                    current = self._clean_response("".join(current_message_parts))
                    if current:
                        parts.append(current)
                    return "\n\n".join(part for part in parts if part)

                async def consume_agent_events() -> None:  # noqa: C901 -- lifecycle router
                    """Consume one turn's events with a terminal-event stall guard.

                    The same lifecycle invariant as the Claude reader (#411 C):
                    once answer text exists and no tool or approval is pending,
                    prolonged silence means the completion event vanished — stop
                    consuming after the bounded grace instead of holding the
                    conversation until the full process timeout.
                    """
                    nonlocal terminal_error, stalled, admission_stalled
                    nonlocal pending_completed_message
                    nonlocal attempt_recorded
                    busy_depth = 0
                    approval_pending = False
                    admitted = False
                    active_tools: dict[str, str] = {}
                    iterator = session.send_turn(
                        user_message,
                        approval_handler=handle_approval,
                    ).__aiter__()

                    async def next_event(timeout: float) -> tuple[bool, Any]:
                        """Wait without cancelling the iterator before interrupt.

                        asyncio.wait_for() cancels ``__anext__`` before raising its
                        timeout. Both real runtimes clear their active-turn entry
                        during that cancellation, making the later interrupt a
                        no-op. Keep the next-event task alive long enough to
                        interrupt the owning provider turn first, then cancel and
                        close the iterator (#625).
                        """

                        pending = asyncio.create_task(iterator.__anext__())
                        try:
                            done, _ = await asyncio.wait((pending,), timeout=timeout)
                            if done:
                                return False, pending.result()
                            await self._interrupt_agent_session(session)
                            # Once the real owner has received its interrupt,
                            # remove this waiter from the old shared lock before
                            # closing/rotating the owner. Otherwise releasing
                            # that lock can let the timed-out prompt slip into
                            # the poisoned session during abort cleanup.
                            pending.cancel()
                            await asyncio.gather(pending, return_exceptions=True)
                            abort_stalled_turn = getattr(
                                session, "abort_stalled_turn", None
                            )
                            if callable(abort_stalled_turn):
                                try:
                                    await asyncio.wait_for(
                                        abort_stalled_turn(),
                                        timeout=self._agent_interrupt_timeout_seconds,
                                    )
                                except TimeoutError:
                                    logger.warning(
                                        "Agent stalled-turn abort timed out after %.1fs",
                                        self._agent_interrupt_timeout_seconds,
                                    )
                                except Exception:
                                    logger.exception(
                                        "Failed to abort stalled agent turn owner"
                                    )
                            return True, None
                        except asyncio.CancelledError:
                            pending.cancel()
                            await asyncio.gather(pending, return_exceptions=True)
                            raise

                    try:
                        while True:
                            stall_eligible = (
                                stall_grace > 0
                                and bool(text_parts)
                                and busy_depth <= 0
                                and not approval_pending
                            )
                            try:
                                if not admitted and admission_grace > 0:
                                    timed_out, event = await next_event(admission_grace)
                                elif stall_eligible:
                                    timed_out, event = await next_event(stall_grace)
                                else:
                                    timed_out = False
                                    event = await iterator.__anext__()
                            except StopAsyncIteration:
                                return
                            if timed_out:
                                if admitted:
                                    stalled = True
                                else:
                                    admission_stalled = True
                                return
                            now = asyncio.get_running_loop().time()
                            if not admitted:
                                admitted = True
                                progress_request.waiting_for_turn = False
                                self._agent_waiting_for_turn.discard(key)
                                ledger = self._task_ledger
                                if ledger and progress_request.task_id:
                                    ledger.set_state(
                                        progress_request.task_id, TASK_WORKING
                                    )
                            progress_request.last_event_at = now
                            if not attempt_recorded:
                                # Claude adapter-path spend boundary (#388):
                                # ClaudeRuntime has no turn-attempt seam, so
                                # the first event of an accepted turn meters
                                # the request. No-op for runtimes (Codex)
                                # that meter at their own boundary.
                                attempt_recorded = True
                                self.record_claude_adapter_attempt(mode=usage_mode)
                            approval_pending = isinstance(event, ApprovalRequestEvent)
                            if isinstance(event, TextDeltaEvent):
                                # A new text delta after a completed message proves
                                # the prior message was interim rather than final.
                                await deliver_pending_interim()
                                progress_request.last_text_at = now
                                text_parts.append(event.text)
                                current_message_parts.append(event.text)
                                if streaming_handler:
                                    await streaming_handler.update_if_needed(event.text)
                                    progress_request.last_visible_progress_at = now
                            elif isinstance(event, MessageCompletedEvent):
                                completed = self._clean_response(
                                    "".join(current_message_parts)
                                )
                                current_message_parts.clear()
                                if completed:
                                    pending_completed_message = completed
                            elif isinstance(event, ToolStartedEvent):
                                # Look ahead by one meaningful lifecycle event:
                                # tool work means the completed text should be a
                                # separate bubble now, not at turn completion.
                                await deliver_pending_interim()
                                busy_depth += 1
                                progress_request.last_tool_at = now
                                label = tool_label(event.tool_name, dict(event.arguments))
                                if label is not None:
                                    active_tools[event.tool_call_id] = label
                                    progress_request.current_tool_label = label
                                if streaming_handler:
                                    await streaming_handler.add_tool_call(
                                        event.tool_name,
                                        dict(event.arguments),
                                    )
                                    progress_request.last_visible_progress_at = now
                            elif isinstance(event, ToolCompletedEvent):
                                busy_depth = max(0, busy_depth - 1)
                                active_tools.pop(event.tool_call_id, None)
                                progress_request.current_tool_label = (
                                    list(active_tools.values())[-1]
                                    if active_tools
                                    else None
                                )
                            elif isinstance(event, ErrorEvent):
                                terminal_error = event
                            elif isinstance(event, ResultEvent):
                                # Terminal usage payload: the Claude adapter
                                # path meters its tokens here (#388); a no-op
                                # for Codex, which meters via the runtime's
                                # usage-recorder seam.
                                self.record_claude_adapter_result(event, mode=usage_mode)
                            elif isinstance(
                                event,
                                (
                                    ReasoningDeltaEvent,
                                    ApprovalRequestEvent,
                                    CompletionEvent,
                                ),
                            ):
                                # Reasoning remains private. Other normalized
                                # lifecycle events are consumed so provider
                                # objects never escape.
                                continue
                    finally:
                        # Request metering happens at the runtime's spend
                        # boundary (turn/start accepted), not here (#388).
                        # Run the generator's cleanup (turn bookkeeping, locks)
                        # even when the stall guard abandoned it mid-turn; this
                        # also guarantees a late completion event has no
                        # consumer left, so the answer cannot deliver twice.
                        try:
                            await iterator.aclose()
                        except Exception:
                            pass

                await asyncio.wait_for(
                    consume_agent_events(), timeout=self._process_timeout_seconds
                )

                if admission_stalled:
                    progress_terminal_state = TASK_TIMEOUT
                    self._drop_agent_session(key, session)
                    logger.warning(
                        "Turn admission timed out for user %s chat %s before the "
                        "runtime produced its first event",
                        user_id,
                        chat_id,
                    )
                    try:
                        health_reporter.record_stalled_request()
                    except Exception:
                        pass
                    message = (
                        f"Agent turn did not start within {admission_grace:g}s"
                    )
                    return ChatResponse(
                        content=f"⏰ {message}. Please retry your request.",
                        success=False,
                        error=message,
                        session_id=session.session_id,
                    )

                if stalled:
                    progress_terminal_state = TASK_COMPLETED
                    self._drop_agent_session(key, session)
                    final_streamed = False
                    if streaming_handler:
                        final_streamed = await streaming_handler.finalize_all()
                    logger.warning(
                        "Terminal-event stall released agent turn for user %s chat %s "
                        "after silence following answer text",
                        user_id,
                        chat_id,
                    )
                    try:
                        health_reporter.record_stalled_request()
                    except Exception:
                        pass
                    content = response_content()
                    streamed = final_streamed
                    if not content and interim_delivered:
                        streamed = True
                    content = content or "(No response)"
                    return ChatResponse(
                        content=f"{content}\n\n{TERMINAL_STALL_NOTICE}",
                        success=True,
                        session_id=session.session_id,
                        streamed=streamed,
                    )

                final_streamed = False
                if streaming_handler:
                    final_streamed = await streaming_handler.finalize_all()
                content = response_content()
                streamed = final_streamed
                if not content and interim_delivered:
                    streamed = True
                content = content or "(No response)"
                if terminal_error is not None:
                    progress_terminal_state = TASK_FAILED
                    self._drop_agent_session(key, session)
                    return ChatResponse(
                        content=f"❌ Processing failed: {terminal_error.message}",
                        success=False,
                        error=terminal_error.message,
                        session_id=session.session_id,
                        # The error itself was not part of the assistant draft.
                        # Always deliver it even when interim text was streamed.
                        streamed=False,
                    )
                progress_terminal_state = TASK_COMPLETED
                return ChatResponse(
                    content=content,
                    success=True,
                    session_id=session.session_id,
                    streamed=streamed,
                )
            except TimeoutError:
                progress_terminal_state = TASK_TIMEOUT
                if session is not None:
                    self._drop_agent_session(key, session)
                if session is not None:
                    await self._interrupt_agent_session(session)
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
                progress_terminal_state = TASK_CANCELED
                if session is not None:
                    await self._interrupt_agent_session(session)
                await self._cancel_agent_streaming(
                    streaming_handler, context="propagating task cancellation"
                )
                raise
            except Exception as exc:
                progress_terminal_state = TASK_FAILED
                if session is not None:
                    self._drop_agent_session(key, session)
                await self._cancel_agent_streaming(
                    streaming_handler, context="returning an agent error"
                )
                message = str(exc) or "Agent runtime failed"
                return ChatResponse(
                    content=f"❌ Error: {message}",
                    success=False,
                    error=message,
                    session_id=session.session_id if session is not None else session_id,
                )
            finally:
                if not progress_future.done():
                    progress_future.set_result(None)
                try:
                    await asyncio.wait_for(progress_task, timeout=5.0)
                except TimeoutError:
                    progress_task.cancel()
                    await asyncio.gather(progress_task, return_exceptions=True)
                cleaned = await self._cleanup_heartbeat(progress_request)
                self._ledger_finish(
                    progress_request,
                    progress_terminal_state,
                    cleanup_done=cleaned,
                )
                if self._agent_active_generations.get(key) == generation:
                    self._agent_active_generations.pop(key, None)
                if self._agent_active_sessions.get(key) is session:
                    self._agent_active_sessions.pop(key, None)
                    self._agent_started_at.pop(key, None)
                self._agent_waiting_for_turn.discard(key)
