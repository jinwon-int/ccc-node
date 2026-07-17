"""Reader-loop mixin for ProjectChatHandler."""

# mypy: disable-error-code="attr-defined"

import asyncio
import logging
import os

from claude_agent_sdk import (
    AssistantMessage,
    RateLimitEvent,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ToolUseBlock,
)

from telegram_bot.core.heartbeat import tool_label
from telegram_bot.core.task_ledger import (
    COMPLETED as TASK_COMPLETED,
    FAILED as TASK_FAILED,
)
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
    @staticmethod
    def _claude_message_boundaries_enabled(req) -> bool:
        return bool(req.streaming_handler or req.interim_message_callback)

    async def _deliver_pending_claude_interim(self, req) -> None:
        """Deliver one completed Claude message once later work proves it interim."""
        content = req.pending_completed_message
        if content is None:
            return

        delivered = False
        if req.streaming_handler is not None:
            try:
                delivered = bool(await req.streaming_handler.finalize_segment())
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Claude interim streaming finalization failed; retaining it "
                    "for final delivery"
                )
        elif req.interim_message_callback is not None:
            try:
                await req.interim_message_callback(content)
                delivered = True
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Claude interim assistant message delivery failed; retaining "
                    "it for final delivery"
                )

        if delivered:
            req.interim_delivered = True
            req.delivered_interim_parts.append(content)
        else:
            req.retained_response_parts.append(content)
        req.pending_completed_message = None

    def _claude_response_content(self, req, result_text: str) -> str:
        """Build final text without duplicating successfully delivered bubbles."""
        terminal = self._clean_response(result_text)
        if not terminal and req.pending_completed_message is not None:
            terminal = req.pending_completed_message
        if not terminal:
            terminal = self._clean_response("\n".join(req.last_assistant_texts))
        if (
            terminal
            and req.pending_completed_message is None
            and terminal in req.delivered_interim_parts
        ):
            # A tool-only tail can repeat the already delivered progress text
            # in ResultMessage.result without emitting a later AssistantMessage.
            terminal = ""

        parts = list(req.retained_response_parts)
        if terminal and (not parts or parts[-1] != terminal):
            parts.append(terminal)
        return "\n\n".join(part for part in parts if part)

    async def _handle_unsolicited_message(
        self, user_id: int, state: _UserStreamState, msg
    ) -> None:
        """Route a background SDK result after the request FIFO has drained.

        Assistant text is buffered until its terminal ResultMessage so the
        Telegram route receives one complete message, not both SDK frames.
        StreamEvent partials are intentionally ignored because there is no live
        Telegram draft associated with an unsolicited notification.
        """
        if isinstance(msg, ResultMessage) and state.stall_swallow_result:
            # A terminal-event stall already delivered this turn's buffered
            # text and the stream is being torn down — swallow the late
            # terminal frame so the answer is not delivered twice (#411 C).
            state.stall_swallow_result = False
            logger.warning(
                "Swallowed late ResultMessage after terminal-event stall release: "
                "user=%s session=%s",
                user_id,
                msg.session_id,
            )
            return
        if isinstance(msg, StreamEvent):
            # The first token delta establishes turn ownership even though
            # unsolicited drafts are intentionally not streamed to Telegram.
            state.unsolicited_inflight = True
            return
        if isinstance(msg, AssistantMessage):
            state.unsolicited_inflight = True
            state.unsolicited_assistant_texts.extend(
                block.text for block in msg.content if isinstance(block, TextBlock)
            )
            return
        if not isinstance(msg, ResultMessage):
            return

        state.last_session_id = msg.session_id or state.last_session_id
        raw = msg.result or "\n".join(state.unsolicited_assistant_texts)
        state.unsolicited_assistant_texts.clear()
        state.unsolicited_inflight = False
        content = getattr(self, "_clean_response")(raw) or "(No response)"
        if msg.is_error:
            content = f"❌ Background task failed: {content}"

        callback = state.unsolicited_callback
        if callback is None:
            logger.warning(
                "Dropping unsolicited SDK result without Telegram route: user=%s session=%s",
                user_id,
                msg.session_id,
            )
            return
        try:
            await callback(content, msg.session_id)
        except Exception as exc:
            logger.error(
                "Unsolicited Telegram delivery failed: user=%s session=%s error=%s",
                user_id,
                msg.session_id,
                type(exc).__name__,
            )
            health_reporter.record_claude_error(
                f"Unsolicited Telegram delivery failed: {type(exc).__name__}"
            )
            return

        if msg.is_error:
            health_reporter.record_claude_error(content)
            state.last_error = content
            state.last_error_ts = self._clock.monotonic()
        else:
            health_reporter.record_claude_ok()
        log_chat(
            user_id,
            msg.session_id,
            "assistant",
            content,
            model=state.model,
            success=not msg.is_error,
        )

    async def _reader_loop(self, user_id: int, state: _UserStreamState) -> None:  # noqa: C901 -- #348 baseline hotspot
        try:
            async for msg in state.client.receive_messages():
                if isinstance(msg, RateLimitEvent):
                    # Account-level signal, unrelated to any pending Telegram
                    # request — record it and move on regardless of pending
                    # state (see `_record_claude_rate_limit`).
                    self._record_claude_rate_limit(msg)
                    continue

                if state.unsolicited_inflight or not state.pending:
                    await self._handle_unsolicited_message(user_id, state, msg)
                    continue

                req = state.pending[0]
                now = asyncio.get_event_loop().time()
                # Any SDK event means the stream is alive; reset the stall clock
                # so the heartbeat keeps ticking. Silence resumes the countdown.
                req.last_event_at = now
                # Spend boundary (#388): the first SDK event proves this
                # attempt reached the provider, so one request is metered
                # here — surviving reader crashes and cancellations. Frames
                # carrying usage meter their positive delta immediately, so
                # a lost terminal result keeps the observed tokens and the
                # ResultMessage reconciles the remainder without doubling.
                self.record_claude_attempt(req)
                self.record_claude_observed_usage(req, msg)
                if (
                    req.typing_callback
                    and not isinstance(msg, ResultMessage)
                    and self._should_refresh_typing(req, now)
                    and now - req.last_typing_at >= self._typing_interval_seconds
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
                            # A text delta after a completed AssistantMessage is
                            # one-event look-ahead proof that the prior message
                            # was interim rather than terminal.
                            await self._deliver_pending_claude_interim(req)
                            req.streamed_via_partials = True
                            req.current_message_streamed_via_partials = True
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
                    if (
                        self._claude_message_boundaries_enabled(req)
                        and req.pending_completed_message is not None
                    ):
                        # A new completed SDK message proves the previous one
                        # was not the terminal answer.
                        await self._deliver_pending_claude_interim(req)
                    req.last_assistant_texts = []
                    text_blocks = [
                        block for block in msg.content if isinstance(block, TextBlock)
                    ]
                    tool_blocks = [
                        block for block in msg.content if isinstance(block, ToolUseBlock)
                    ]
                    for text_block in text_blocks:
                        logger.debug(f"TextBlock: {len(text_block.text)} chars")
                        req.last_text_at = now
                        req.last_assistant_texts.append(text_block.text)
                        # Update the streaming draft from the complete block
                        # ONLY when partial deltas didn't already build it —
                        # otherwise the text would be doubled. When partial
                        # streaming is off (no deltas), this is the fallback
                        # whole-block update path.
                        if (
                            req.streaming_handler
                            and not req.current_message_streamed_via_partials
                        ):
                            try:
                                await req.streaming_handler.update_if_needed(
                                    text_block.text
                                )
                                req.last_visible_progress_at = now
                            except Exception as e:
                                logger.error(f"Streaming update failed: {e}")
                        if os.environ.get("BOT_DEBUG"):
                            print(
                                f"\033[36m[Claude]\033[0m {text_block.text[:200]}"
                            )

                    if self._claude_message_boundaries_enabled(req):
                        completed = self._clean_response(
                            "\n".join(req.last_assistant_texts)
                        )
                        if completed:
                            req.pending_completed_message = completed

                    # A tool in the same AssistantMessage proves any preceding
                    # user-visible text is interim. Finalize that bubble before
                    # adding the tool status to the next streaming segment.
                    if tool_blocks and req.pending_completed_message is not None:
                        await self._deliver_pending_claude_interim(req)

                    for tool_block in tool_blocks:
                        logger.debug(f"ToolUseBlock: {tool_block.name}")
                        req.last_tool_at = now
                        req.current_tool_label = tool_label(
                            tool_block.name, tool_block.input
                        )
                        if req.streaming_handler:
                            try:
                                await req.streaming_handler.add_tool_call(
                                    tool_block.name, tool_block.input
                                )
                                req.last_visible_progress_at = now
                            except Exception as e:
                                logger.error(f"Tool call display failed: {e}")
                        if os.environ.get("BOT_DEBUG"):
                            print(
                                f"\033[33m[Tool: {tool_block.name}]\033[0m "
                                f"{str(tool_block.input)[:150]}"
                            )
                    req.current_message_streamed_via_partials = False
                    continue

                if isinstance(msg, ResultMessage):
                    state.last_session_id = msg.session_id or state.last_session_id
                    self._record_claude_usage(req, msg)
                    result_text = msg.result or "\n".join(req.last_assistant_texts)

                    # Finalize streaming drafts
                    final_streamed = False
                    if req.streaming_handler:
                        try:
                            final_streamed = bool(
                                await req.streaming_handler.finalize_all()
                            )
                        except Exception as e:
                            logger.error(f"Streaming finalization failed: {e}")
                    cleaned = await self._cleanup_heartbeat(req)
                    # Terminal transition: a failed cleanup keeps a retryable op
                    # in the ledger, so the status message can never be orphaned.
                    self._ledger_finish(
                        req,
                        TASK_FAILED if msg.is_error else TASK_COMPLETED,
                        cleanup_done=cleaned,
                    )

                    if req.synthetic_response:
                        content = (
                            self._clean_response(req.synthetic_response)
                            or "(No response)"
                        )
                    else:
                        content = (
                            self._claude_response_content(req, result_text)
                            or "(No response)"
                        )

                    logger.info(
                        f"ResultMessage: session={msg.session_id}, is_error={msg.is_error}, duration={msg.duration_ms}ms"
                    )
                    self._append_duration_log(req, msg)

                    if msg.is_error:
                        logger.error(f"SDK returned error: {content[:500]}")
                        health_reporter.record_claude_error(content)
                        # Record the cause so a racing disconnect can surface it
                        # instead of the opaque "Task has been terminated." notice.
                        state.last_error = content
                        state.last_error_ts = self._clock.monotonic()
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
                            streamed=False,
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
                        # ``streamed`` means the caller must not send the final
                        # content again. Options still use their separate path.
                        is_streamed = final_streamed
                        if content == "(No response)" and req.interim_delivered:
                            is_streamed = True
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
            # Record the cause so a racing disconnect can surface it instead of
            # the opaque "Task has been terminated." notice.
            state.last_error = str(e)
            state.last_error_ts = self._clock.monotonic()
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
                cleaned = await self._cleanup_heartbeat(req)
                self._ledger_finish(req, TASK_FAILED, cleanup_done=cleaned)
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
