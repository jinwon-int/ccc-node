"""Message submission/retry mixin for ProjectChatHandler."""

# mypy: disable-error-code="attr-defined"

import asyncio
import logging
from typing import Any, Optional

from telegram_bot.core.project_chat_types import (
    ChatResponse,
    PermissionCallback,
    StatusCallback,
    TypingCallback,
    _PendingRequest,
    _UserStreamState,
)
from telegram_bot.core.sdk_text import (
    RESTART_INTERRUPT_NOTICE,
    _is_retryable_sdk_error,
    _is_shutdown_signal_error,
)
from telegram_bot.utils.chat_logger import log_chat
from telegram_bot.utils.config import config
from telegram_bot.utils.health import health_reporter

logger = logging.getLogger(__name__)


def _process_timeout() -> int:
    """Read the compatibility constant from project_chat at call time."""
    from telegram_bot.core import project_chat

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
        new_session: bool = False,
        permission_callback: Optional[PermissionCallback] = None,
        typing_callback: Optional[TypingCallback] = None,
        status_callback: Optional[StatusCallback] = None,
        bot: Optional[Any] = None,
    ) -> ChatResponse:
        del message_id
        logger.info(f"Processing message from user {user_id}: {user_message[:80]}...")
        log_chat(user_id, session_id, "user", user_message, model=model)

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()

        # Create streaming handler only when live streaming is enabled. With it
        # off (default), the reply is delivered as complete message(s) by the
        # caller when generation finishes — no live draft.
        streaming_handler = None
        if bot and getattr(config, "enable_streaming", False):
            from telegram_bot.core.streaming import StreamingMessageHandler

            streaming_handler = StreamingMessageHandler(bot, chat_id, user_id)

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
        state: Optional[_UserStreamState] = None

        try:
            state = await self._get_or_create_stream(user_id, chat_id, model, new_session)
            async with state.send_lock:
                request.sent_session_id = (
                    session_id or state.last_session_id or "default"
                )
                state.pending.append(request)
                await state.client.query(
                    user_message, session_id=request.sent_session_id
                )
                logger.info(
                    f"Submitted message to live stream: user={user_id}, pending={len(state.pending)}, "
                    f"session_key={request.sent_session_id}"
                )
                if config.claude_cli_path:
                    logger.info(
                        f"Using configured Claude CLI path: {config.claude_cli_path}"
                    )

            return await asyncio.wait_for(future, timeout=_process_timeout())

        except asyncio.CancelledError:
            logger.info(f"Task cancelled for user {user_id} - cleaning up")
            # Clean up streaming drafts if active
            if streaming_handler:
                try:
                    await streaming_handler.cancel()
                except Exception as e:
                    logger.error(f"Failed to cancel streaming handler: {e}")
            await self._cleanup_heartbeat(request)
            await self.stop(user_id)
            # Don't return a message - bot.py will handle the user response
            raise

        except asyncio.TimeoutError:
            logger.warning(
                f"Query timed out for user {user_id} after {_process_timeout()}s"
            )
            await self._cleanup_heartbeat(request)
            await self.stop(user_id)
            msg = f"⏰ Timed out after {_process_timeout()}s. Please retry or simplify your request."
            health_reporter.record_claude_error(msg)
            return ChatResponse(content=msg, success=False, error=msg)

        except Exception as e:
            if state and request in state.pending:
                try:
                    state.pending.remove(request)
                except ValueError:
                    pass
            await self._cleanup_heartbeat(request)

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
                if bot and getattr(config, "enable_streaming", False):
                    from telegram_bot.core.streaming import StreamingMessageHandler

                    retry_handler = StreamingMessageHandler(bot, chat_id, user_id)
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
                retry_state: Optional[_UserStreamState] = None
                try:
                    retry_state = await self._get_or_create_stream(
                        user_id, chat_id, model, new_session=False
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
                    return await asyncio.wait_for(retry_future, timeout=_process_timeout())
                except Exception as retry_err:
                    if retry_state and retry_request in retry_state.pending:
                        try:
                            retry_state.pending.remove(retry_request)
                        except ValueError:
                            pass
                    await self._cleanup_heartbeat(retry_request)
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
