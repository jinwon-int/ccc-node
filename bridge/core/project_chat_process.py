"""Message submission/retry mixin for ProjectChatHandler."""

# mypy: disable-error-code="attr-defined"

import asyncio
import logging
import re
from collections.abc import Mapping
from typing import Any, Optional

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
from telegram_bot.core.project_chat_types import (
    AgentApprovalCallback,
    AgentSessionEntry,
    ChatResponse,
    InterimMessageCallback,
    PermissionCallback,
    StatusCallback,
    TypingCallback,
    UnsolicitedCallback,
    _PendingRequest,
    _UserStreamState,
)
from telegram_bot.core.task_ledger import (
    CANCELED as TASK_CANCELED,
    COMPLETED as TASK_COMPLETED,
    FAILED as TASK_FAILED,
    INPUT_REQUIRED as TASK_INPUT_REQUIRED,
    TIMEOUT as TASK_TIMEOUT,
    WORKING as TASK_WORKING,
)
from telegram_bot.core.sdk_text import (
    RESTART_INTERRUPT_NOTICE,
    TERMINAL_STALL_NOTICE,
    _is_retryable_sdk_error,
    _is_shutdown_signal_error,
)
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


def _process_timeout() -> int:
    """Read the active project_chat module compatibility constant at call time."""
    import sys

    project_chat = sys.modules["telegram_bot.core.project_chat"]
    return getattr(project_chat, "PROCESS_TIMEOUT", 21600)


class ProjectChatProcessMixin:
    async def process_message(  # noqa: C901 -- #348 baseline hotspot
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
    ) -> ChatResponse:
        del message_id
        if self._agent_runtime is not None:
            if getattr(self._config, "agent_provider", "claude") == "claude":
                # Claude adapter path (#584 slice B, CCC_CLAUDE_RUNTIME_ADAPTER):
                # the bot layer's approval/sandbox knobs are Codex app-server
                # policies (bot_access._codex_*) that ClaudeRuntime rejects
                # fail-closed. On this path the approval boundary is the SDK
                # can_use_tool -> approval_callback seam, so drop the
                # untranslatable Codex-only knobs instead of forwarding them.
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
            )
        _log_user_input(
            user_message=user_message,
            user_id=user_id,
            session_id=session_id,
            model=model,
            sensitive_log_event=sensitive_log_event,
        )

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()

        # Create streaming handler only when live streaming is enabled. With it
        # off (default), the reply is delivered as complete message(s) by the
        # caller when generation finishes — no live draft.
        streaming_handler = None
        if bot and getattr(self._config, "enable_streaming", False):
            from telegram_bot.core.streaming import StreamingMessageHandler

            streaming_handler = StreamingMessageHandler(
                bot, chat_id, user_id, settings=self._config
            )

        request = _PendingRequest(
            user_id=user_id,
            chat_id=chat_id,
            model=model,
            requested_session_id=session_id,
            permission_callback=permission_callback,
            typing_callback=typing_callback,
            future=future,
            status_callback=status_callback,
            streaming_handler=streaming_handler,
            interim_message_callback=interim_message_callback,
        )
        request.started_at = loop.time()
        request.task_id = self._ledger_create(user_id, chat_id)
        state: Optional[_UserStreamState] = None

        unsolicited_callback: Optional[UnsolicitedCallback] = None
        route_bot = notification_bot or bot
        if route_bot:

            async def deliver_unsolicited(
                content: str, _session_id: Optional[str]
            ) -> None:
                # Background task notifications have no caller waiting to send
                # the ChatResponse. Keep one SDK result to one Telegram message.
                text = content
                if len(text) > 4000:
                    text = f"{text[:3960]}\n\n… (background result truncated)"
                await route_bot.send_message(chat_id=chat_id, text=text)

            unsolicited_callback = deliver_unsolicited

        # The Claude Code SDK can internally queue follow-up prompts on a
        # live stream, so submitting a second query before the first
        # ResultMessage arrives breaks the bridge's pending FIFO. Serialize
        # the full send→result window per Telegram conversation.
        conversation_lock = self._get_conversation_lock(user_id, chat_id)
        async with conversation_lock:
            try:
                state = await getattr(self, "_get_or_create_stream")(
                    user_id, chat_id, model, new_session, unsolicited_callback
                )
                async with state.send_lock:
                    request.sent_session_id = session_id or state.last_session_id or "default"
                    state.pending.append(request)
                    await state.client.query(user_message, session_id=request.sent_session_id)
                    logger.info(
                        f"Submitted message to live stream: user={user_id}, pending={len(state.pending)}, "
                        f"session_key={request.sent_session_id}"
                    )
                    if self._config.claude_cli_path:
                        logger.info(
                            "Using configured Claude CLI path: %s",
                            self._config.claude_cli_path,
                        )

                return await asyncio.wait_for(future, timeout=self._process_timeout_seconds)

            except asyncio.CancelledError:
                logger.info(f"Task cancelled for user {user_id} - cleaning up")
                # Clean up streaming drafts if active
                if streaming_handler:
                    try:
                        await streaming_handler.cancel()
                    except Exception as e:
                        logger.error(f"Failed to cancel streaming handler: {e}")
                cleaned = await self._cleanup_heartbeat(request)
                self._ledger_finish(request, TASK_CANCELED, cleanup_done=cleaned)
                await self.stop(user_id)
                # Don't return a message - bot.py will handle the user response
                raise

            except asyncio.TimeoutError:
                logger.warning(f"Query timed out for user {user_id} after {self._process_timeout_seconds}s")
                cleaned = await self._cleanup_heartbeat(request)
                self._ledger_finish(request, TASK_TIMEOUT, cleanup_done=cleaned)
                await self.stop(user_id)
                msg = f"⏰ Timed out after {self._process_timeout_seconds}s. Please retry or simplify your request."
                health_reporter.record_claude_error(msg)
                return ChatResponse(content=msg, success=False, error=msg)

            except Exception as e:
                if state and request in state.pending:
                    try:
                        state.pending.remove(request)
                    except ValueError:
                        pass
                cleaned = await self._cleanup_heartbeat(request)
                self._ledger_finish(request, TASK_FAILED, cleanup_done=cleaned)

                err = str(e)
                logger.error(
                    f"SDK error for user {user_id}: {err} (type: {type(e).__name__})",
                    exc_info=True,
                )

                # Retry once for transient SDK errors (network/timeout errors)
                is_retryable = _is_retryable_sdk_error(e)
                logger.info(
                    f"Error retryability check for user {user_id}: "
                    f"is_retryable={is_retryable}, error='{err[:100]}...'"
                )

                if is_retryable:
                    logger.warning(
                        "Retryable SDK error for user %s: %s — reconnecting and retrying",
                        user_id,
                        err,
                    )
                    logger.info(f"Disconnecting stream for user {user_id} before retry...")
                    await self._disconnect_user_stream(user_id, chat_id)
                    logger.info(
                        f"Stream disconnected for user {user_id}, creating retry request..."
                    )

                    retry_future: asyncio.Future = loop.create_future()
                    retry_handler = None
                    if bot and getattr(self._config, "enable_streaming", False):
                        from telegram_bot.core.streaming import StreamingMessageHandler

                        retry_handler = StreamingMessageHandler(
                bot, chat_id, user_id, settings=self._config
            )
                    retry_request = _PendingRequest(
                        user_id=user_id,
                        chat_id=chat_id,
                        model=model,
                        requested_session_id=session_id,
                        permission_callback=permission_callback,
                        typing_callback=typing_callback,
                        future=retry_future,
                        status_callback=status_callback,
                        streaming_handler=retry_handler,
                        interim_message_callback=interim_message_callback,
                    )
                    retry_request.started_at = loop.time()
                    retry_request.task_id = self._ledger_create(user_id, chat_id)
                    retry_state: Optional[_UserStreamState] = None
                    try:
                        retry_state = await getattr(self, "_get_or_create_stream")(
                            user_id,
                            chat_id,
                            model,
                            False,
                            unsolicited_callback,
                        )
                        async with retry_state.send_lock:
                            retry_request.sent_session_id = (
                                session_id or retry_state.last_session_id or "default"
                            )
                            retry_state.pending.append(retry_request)
                            await retry_state.client.query(
                                user_message, session_id=retry_request.sent_session_id
                            )
                            logger.info(
                                "✅ Retry submitted successfully for user %s after reconnection",
                                user_id,
                            )
                        return await asyncio.wait_for(retry_future, timeout=self._process_timeout_seconds)
                    except Exception as retry_err:
                        if retry_state and retry_request in retry_state.pending:
                            try:
                                retry_state.pending.remove(retry_request)
                            except ValueError:
                                pass
                        cleaned = await self._cleanup_heartbeat(retry_request)
                        self._ledger_finish(retry_request, TASK_FAILED, cleanup_done=cleaned)
                        if not retry_future.done():
                            retry_future.cancel()
                        logger.error(
                            "Retry also failed for user %s: %s",
                            user_id,
                            retry_err,
                            exc_info=True,
                        )
                        retry_msg = str(retry_err)
                        health_reporter.record_claude_error(retry_msg)
                        return ChatResponse(
                            content=(
                                RESTART_INTERRUPT_NOTICE
                                if _is_shutdown_signal_error(retry_msg)
                                else f"❌ Error: {retry_msg}"
                            ),
                            success=False,
                            error=retry_msg,
                        )

                logger.error(f"Error processing message: {e}", exc_info=True)
                health_reporter.record_claude_error(err)
                return ChatResponse(
                    content=(
                        RESTART_INTERRUPT_NOTICE
                        if _is_shutdown_signal_error(err)
                        else f"❌ Error: {err}"
                    ),
                    success=False,
                    error=err,
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

        The adapter counterpart of the direct path's ``deliver_unsolicited``
        closure plus ``_handle_unsolicited_message`` bookkeeping: assistant
        output the runtime produced outside any active turn (for example the
        CLI autonomously continuing after a background-task notification) is
        cleaned, bounded, and sent to the same conversation. The seam is
        optional — sessions without ``set_unsolicited_handler`` (Codex) keep
        their current behavior — and without a route bot any previously
        registered handler is left in place, mirroring how the direct path
        keeps an existing stream callback when a request carries no bot.
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
            progress_request.started_at = loop.time()
            progress_request.task_id = self._ledger_create(user_id, chat_id)
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
                    session = await self._agent_runtime.start_or_resume(
                        SessionRequest(
                            working_directory=str(self.project_root),
                            session_id=None if new_session else session_id,
                            model=model,
                            effort=effort,
                            approval_policy=approval_policy,
                            approvals_reviewer=approvals_reviewer,
                            sandbox_policy=sandbox_policy,
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
                self._agent_active_sessions[key] = session
                self._agent_started_at[key] = asyncio.get_running_loop().time()
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
                stalled = False
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
                    nonlocal terminal_error, stalled, pending_completed_message
                    nonlocal attempt_recorded
                    busy_depth = 0
                    approval_pending = False
                    active_tools: dict[str, str] = {}
                    iterator = session.send_turn(
                        user_message,
                        approval_handler=handle_approval,
                    ).__aiter__()
                    try:
                        while True:
                            stall_eligible = (
                                stall_grace > 0
                                and bool(text_parts)
                                and busy_depth <= 0
                                and not approval_pending
                            )
                            try:
                                if stall_eligible:
                                    event = await asyncio.wait_for(
                                        iterator.__anext__(), timeout=stall_grace
                                    )
                                else:
                                    event = await iterator.__anext__()
                            except StopAsyncIteration:
                                return
                            except TimeoutError:
                                stalled = True
                                return
                            now = asyncio.get_running_loop().time()
                            progress_request.last_event_at = now
                            if not attempt_recorded:
                                # Claude adapter-path spend boundary (#388):
                                # ClaudeRuntime has no turn-attempt seam, so
                                # the first event of an accepted turn meters
                                # the request. No-op for runtimes (Codex)
                                # that meter at their own boundary.
                                attempt_recorded = True
                                self.record_claude_adapter_attempt()
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
                                self.record_claude_adapter_result(event)
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

                if stalled:
                    progress_terminal_state = TASK_COMPLETED
                    self._drop_agent_session(key, session)
                    await self._interrupt_agent_session(session)
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
