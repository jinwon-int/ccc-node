"""Reader-loop mixin for ProjectChatHandler."""

# mypy: disable-error-code="attr-defined"

import asyncio
import logging
import os

from claude_agent_sdk import AssistantMessage, ResultMessage, StreamEvent, TextBlock, ToolUseBlock

from telegram_bot.core.heartbeat import tool_label
from telegram_bot.core.project_chat_types import ChatResponse, _UserStreamState
from telegram_bot.core.sdk_text import (
    RESTART_INTERRUPT_NOTICE,
    _detect_numbered_options,
    _extract_stream_text_delta,
    _is_shutdown_signal_error,
)
from telegram_bot.utils.chat_logger import log_chat
from telegram_bot.utils.health import health_reporter

logger = logging.getLogger(__name__)


def _typing_interval() -> float:
    """Read the active project_chat module compatibility constant at call time."""
    import sys

    project_chat = sys.modules["telegram_bot.core.project_chat"]
    return getattr(project_chat, "TYPING_INTERVAL", 4)


class ProjectChatReaderMixin:
    async def _reader_loop(self, user_id: int, state: _UserStreamState) -> None:
        try:
            async for msg in state.client.receive_messages():
                if not state.pending:
                    continue

                req = state.pending[0]
                now = asyncio.get_event_loop().time()
                # Any SDK event means the stream is alive; reset the stall clock
                # so the heartbeat keeps ticking. Silence resumes the countdown.
                req.last_event_at = now
                if (
                    req.typing_callback
                    and not isinstance(msg, ResultMessage)
                    and self._should_refresh_typing(req, now)
                    and now - req.last_typing_at >= _typing_interval()
                ):
                    req.last_typing_at = now
                    try:
                        await req.typing_callback()
                    except Exception:
                        pass

                if isinstance(msg, StreamEvent):
                    # Real token-level streaming: drive the live draft from
                    # incremental text deltas as they arrive (true typewriter).
                    # Only top-level assistant text streams to the user; nested
                    # subagent (Task) deltas carry parent_tool_use_id and must
                    # not pollute the main response draft.
                    if (
                        req.streaming_handler
                        and getattr(msg, "parent_tool_use_id", None) is None
                    ):
                        delta = _extract_stream_text_delta(msg.event)
                        if delta:
                            req.streamed_via_partials = True
                            try:
                                await req.streaming_handler.update_if_needed(delta)
                                req.last_visible_progress_at = now
                            except Exception as e:
                                logger.error(f"Partial streaming update failed: {e}")
                    continue

                if isinstance(msg, AssistantMessage):
                    logger.debug(
                        f"Received AssistantMessage with {len(msg.content)} blocks"
                    )
                    req.last_assistant_texts = []
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            logger.debug(f"TextBlock: {len(block.text)} chars")
                            req.last_assistant_texts.append(block.text)
                            # Update the streaming draft from the complete block
                            # ONLY when partial deltas didn't already build it —
                            # otherwise the text would be doubled. When partial
                            # streaming is off (no deltas), this is the fallback
                            # whole-block update path.
                            if req.streaming_handler and not req.streamed_via_partials:
                                try:
                                    await req.streaming_handler.update_if_needed(
                                        block.text
                                    )
                                    req.last_visible_progress_at = now
                                except Exception as e:
                                    logger.error(f"Streaming update failed: {e}")
                            if os.environ.get("BOT_DEBUG"):
                                print(f"\033[36m[Claude]\033[0m {block.text[:200]}")
                        elif isinstance(block, ToolUseBlock):
                            logger.debug(f"ToolUseBlock: {block.name}")
                            req.current_tool_label = tool_label(block.name, block.input)
                            if req.streaming_handler:
                                try:
                                    await req.streaming_handler.add_tool_call(
                                        block.name, block.input
                                    )
                                    req.last_visible_progress_at = now
                                except Exception as e:
                                    logger.error(f"Tool call display failed: {e}")
                            if os.environ.get("BOT_DEBUG"):
                                print(
                                    f"\033[33m[Tool: {block.name}]\033[0m {str(block.input)[:150]}"
                                )
                    continue

                if isinstance(msg, ResultMessage):
                    state.last_session_id = msg.session_id or state.last_session_id
                    result_text = msg.result or "\n".join(req.last_assistant_texts)

                    # Finalize streaming drafts
                    if req.streaming_handler:
                        try:
                            await req.streaming_handler.finalize_all()
                        except Exception as e:
                            logger.error(f"Streaming finalization failed: {e}")
                    await self._cleanup_heartbeat(req)

                    if req.synthetic_response:
                        content = (
                            self._clean_response(req.synthetic_response)
                            or "(No response)"
                        )
                    else:
                        content = self._clean_response(result_text) or "(No response)"

                    logger.info(
                        f"ResultMessage: session={msg.session_id}, is_error={msg.is_error}, duration={msg.duration_ms}ms"
                    )
                    self._append_duration_log(req, msg)

                    if msg.is_error:
                        logger.error(f"SDK returned error: {content[:500]}")
                        health_reporter.record_claude_error(content)
                        log_chat(
                            req.user_id,
                            msg.session_id or req.requested_session_id,
                            "assistant",
                            content,
                            model=req.model,
                            success=False,
                        )
                        response = ChatResponse(
                            content=f"❌ Processing failed: {content}",
                            success=False,
                            error=content,
                            session_id=msg.session_id,
                            streamed=bool(
                                req.streaming_handler and req.streaming_handler.drafts
                            ),
                        )
                    else:
                        health_reporter.record_claude_ok()
                        log_chat(
                            req.user_id,
                            msg.session_id or req.requested_session_id,
                            "assistant",
                            content,
                            model=req.model,
                        )
                        # Check if response contains numbered options (even without synthetic_response)
                        has_options = (
                            req.synthetic_response is not None
                            or _detect_numbered_options(content)
                        )
                        # Message is considered streamed if drafts were created, regardless of options
                        # Options will be sent separately by _reply_smart()/_send_smart()
                        is_streamed = bool(
                            req.streaming_handler and req.streaming_handler.drafts
                        )
                        logger.debug(
                            f"Response ready: has_synthetic={bool(req.synthetic_response)}, has_numbered_options={_detect_numbered_options(content)}, has_options={has_options}, is_streamed={is_streamed}, content_len={len(content)}"
                        )
                        response = ChatResponse(
                            content=content,
                            success=True,
                            session_id=msg.session_id,
                            has_options=has_options,
                            streamed=is_streamed,
                        )

                    if not req.future.done():
                        try:
                            req.future.set_result(response)
                        except Exception as e:
                            logger.error(f"Error setting future result: {e}")
                    state.pending.popleft()
        except asyncio.CancelledError:
            logger.debug(f"Reader loop cancelled for user {user_id}")
            raise
        except Exception as e:
            logger.error(f"Reader loop crashed for user {user_id}: {e}", exc_info=True)
            # Cancel typing keepalive to prevent orphan task
            if state.typing_task and not state.typing_task.done():
                state.typing_task.cancel()
            # Remove broken stream(s) by state identity so only the affected
            # conversation is recreated on the next request.
            for key, stream_state in list(self._streams.items()):
                if stream_state is state:
                    self._streams.pop(key, None)
            # Safely handle pending requests
            pending_copy = list(state.pending)
            state.pending.clear()
            for req in pending_copy:
                await self._cleanup_heartbeat(req)
                # Finalize streaming drafts on error
                if req.streaming_handler:
                    try:
                        await req.streaming_handler.finalize_all()
                    except Exception as finalize_err:
                        logger.error(
                            f"Streaming finalization on error failed: {finalize_err}"
                        )
                err = str(e)
                health_reporter.record_claude_error(err)
                log_chat(
                    req.user_id, req.requested_session_id, "error", err, success=False
                )
                # A bridge restart (systemd SIGTERM) kills the in-flight claude
                # child with exit 143; don't surface that raw code — tell the user
                # to resend instead of replying with "❌ Error: ... exit 143".
                if _is_shutdown_signal_error(err):
                    user_content = RESTART_INTERRUPT_NOTICE
                else:
                    user_content = f"❌ Error: {err}"
                if not req.future.done():
                    try:
                        req.future.set_result(
                            ChatResponse(
                                content=user_content,
                                success=False,
                                error=err,
                                session_id=state.last_session_id,
                            )
                        )
                    except Exception as set_err:
                        logger.error(f"Error setting error result: {set_err}")
