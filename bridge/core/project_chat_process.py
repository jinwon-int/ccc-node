"""Message submission/retry mixin for ProjectChatHandler."""

# mypy: disable-error-code="attr-defined"

import asyncio
import logging
from typing import Any, Optional

from telegram_bot.core.agent_runtime import (
    ApprovalDecision,
    ApprovalRequestEvent,
    CompletionEvent,
    ErrorEvent,
    ReasoningDeltaEvent,
    ResultEvent,
    SessionRequest,
    TextDeltaEvent,
    ToolCompletedEvent,
    ToolStartedEvent,
)
from telegram_bot.core.project_chat_types import (
    AgentApprovalCallback,
    ChatResponse,
    PermissionCallback,
    StatusCallback,
    TypingCallback,
    UnsolicitedCallback,
    _PendingRequest,
    _UserStreamState,
)
from telegram_bot.core.task_ledger import (
    CANCELED as TASK_CANCELED,
    FAILED as TASK_FAILED,
    TIMEOUT as TASK_TIMEOUT,
)
from telegram_bot.core.sdk_text import (
    RESTART_INTERRUPT_NOTICE,
    _is_retryable_sdk_error,
    _is_shutdown_signal_error,
)
from telegram_bot.utils.chat_logger import log_chat
from telegram_bot.utils.health import health_reporter

logger = logging.getLogger(__name__)


def _process_timeout() -> int:
    """Read the active project_chat module compatibility constant at call time."""
    import sys

    project_chat = sys.modules["telegram_bot.core.project_chat"]
    return getattr(project_chat, "PROCESS_TIMEOUT", 21600)


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
        new_session: bool = False,
        permission_callback: Optional[PermissionCallback] = None,
        approval_callback: Optional[AgentApprovalCallback] = None,
        typing_callback: Optional[TypingCallback] = None,
        status_callback: Optional[StatusCallback] = None,
        bot: Optional[Any] = None,
        notification_bot: Optional[Any] = None,
    ) -> ChatResponse:
        del message_id
        if self._agent_runtime is not None:
            return await self._process_agent_message(
                user_message=user_message,
                user_id=user_id,
                chat_id=chat_id,
                session_id=session_id,
                model=model,
                effort=effort,
                new_session=new_session,
                approval_callback=approval_callback,
                typing_callback=typing_callback,
                bot=bot,
            )
        logger.info(f"Processing message from user {user_id}: {user_message[:80]}...")
        log_chat(user_id, session_id, "user", user_message, model=model)

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

    async def _process_agent_message(
        self,
        *,
        user_message: str,
        user_id: int,
        chat_id: int,
        session_id: Optional[str],
        model: Optional[str],
        effort: Optional[str],
        new_session: bool,
        approval_callback: Optional[AgentApprovalCallback],
        typing_callback: Optional[TypingCallback],
        bot: Optional[Any],
    ) -> ChatResponse:
        """Run one provider-neutral turn without changing the Claude SDK path."""
        key = self._stream_key(user_id, chat_id)
        streaming_handler = None
        if bot and getattr(self._config, "enable_streaming", False):
            from telegram_bot.core.streaming import StreamingMessageHandler

            streaming_handler = StreamingMessageHandler(bot, chat_id, user_id, settings=self._config)

        async with self._get_conversation_lock(user_id, chat_id):
            generation = self._next_agent_generation(key)
            self._agent_active_generations[key] = generation
            session = self._agent_sessions.get(key)
            if new_session or (
                session is not None
                and (
                    (session_id is not None and session.session_id != session_id)
                    or self._agent_session_models.get(key) != model
                    or self._agent_session_efforts.get(key) != effort
                )
            ):
                self._agent_sessions.pop(key, None)
                self._agent_session_models.pop(key, None)
                self._agent_session_efforts.pop(key, None)
                session = None
            try:
                if session is None:
                    session = await self._agent_runtime.start_or_resume(
                        SessionRequest(
                            working_directory=str(self.project_root),
                            session_id=None if new_session else session_id,
                            model=model,
                            effort=effort,
                        )
                    )
                    self._agent_sessions[key] = session
                    self._agent_session_models[key] = model
                    self._agent_session_efforts[key] = effort

                self._agent_active_sessions[key] = session
                self._agent_started_at[key] = asyncio.get_running_loop().time()
                text_parts: list[str] = []
                terminal_error: ErrorEvent | None = None

                async def handle_approval(event: ApprovalRequestEvent) -> ApprovalDecision:
                    if approval_callback is None:
                        return ApprovalDecision.DENY
                    if not self.is_agent_approval_active(user_id, chat_id, generation):
                        return ApprovalDecision.DENY
                    try:
                        decision = await approval_callback(chat_id, user_id, event, generation)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception("Provider-neutral approval callback failed")
                        return ApprovalDecision.DENY
                    if (
                        decision is ApprovalDecision.ALLOW
                        and self.is_agent_approval_active(user_id, chat_id, generation)
                    ):
                        return ApprovalDecision.ALLOW
                    return ApprovalDecision.DENY

                async def consume_agent_events() -> None:
                    nonlocal terminal_error
                    async for event in session.send_turn(
                        user_message,
                        approval_handler=handle_approval,
                    ):
                        if typing_callback is not None:
                            try:
                                await typing_callback()
                            except Exception:
                                pass
                        if isinstance(event, TextDeltaEvent):
                            text_parts.append(event.text)
                            if streaming_handler:
                                await streaming_handler.update_if_needed(event.text)
                        elif isinstance(event, ToolStartedEvent):
                            if streaming_handler:
                                await streaming_handler.add_tool_call(
                                    event.tool_name,
                                    dict(event.arguments),
                                )
                        elif isinstance(event, ErrorEvent):
                            terminal_error = event
                        elif isinstance(
                            event,
                            (
                                ReasoningDeltaEvent,
                                ToolCompletedEvent,
                                ApprovalRequestEvent,
                                ResultEvent,
                                CompletionEvent,
                            ),
                        ):
                            # Reasoning remains private. Other normalized lifecycle
                            # events are consumed so provider objects never escape.
                            continue

                await asyncio.wait_for(
                    consume_agent_events(), timeout=self._process_timeout_seconds
                )

                if streaming_handler:
                    await streaming_handler.finalize_all()
                content = self._clean_response("".join(text_parts)) or "(No response)"
                streamed = bool(streaming_handler and streaming_handler.drafts)
                if terminal_error is not None:
                    if self._agent_sessions.get(key) is session:
                        self._agent_sessions.pop(key, None)
                        self._agent_session_models.pop(key, None)
                        self._agent_session_efforts.pop(key, None)
                    return ChatResponse(
                        content=f"❌ Processing failed: {terminal_error.message}",
                        success=False,
                        error=terminal_error.message,
                        session_id=session.session_id,
                        streamed=streamed,
                    )
                return ChatResponse(
                    content=content,
                    success=True,
                    session_id=session.session_id,
                    streamed=streamed,
                )
            except TimeoutError:
                if session is not None and self._agent_sessions.get(key) is session:
                    self._agent_sessions.pop(key, None)
                    self._agent_session_models.pop(key, None)
                    self._agent_session_efforts.pop(key, None)
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
                if session is not None:
                    await self._interrupt_agent_session(session)
                await self._cancel_agent_streaming(
                    streaming_handler, context="propagating task cancellation"
                )
                raise
            except Exception as exc:
                if session is not None and self._agent_sessions.get(key) is session:
                    self._agent_sessions.pop(key, None)
                    self._agent_session_models.pop(key, None)
                    self._agent_session_efforts.pop(key, None)
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
                if self._agent_active_generations.get(key) == generation:
                    self._agent_active_generations.pop(key, None)
                if self._agent_active_sessions.get(key) is session:
                    self._agent_active_sessions.pop(key, None)
                    self._agent_started_at.pop(key, None)
