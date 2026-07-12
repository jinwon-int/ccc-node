"""
Streaming message handler for progressive draft message updates.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional, List

from telegram import Bot
from telegram.error import TelegramError, RetryAfter, BadRequest

from telegram_bot.utils.config import config
from telegram_bot.utils import tg_md, tg_readable, tg_entities

logger = logging.getLogger(__name__)


@dataclass
class DraftState:
    """State for a single draft message"""

    message_id: int
    text: str
    last_update_time: float
    char_count_since_update: int = 0
    draft_id: Optional[str] = None
    part_header_index: Optional[int] = None
    part_header_total: Optional[int] = None


class StreamingMessageHandler:
    """
    Handles progressive streaming of AI responses using Telegram draft messages.

    Manages draft message lifecycle: creation, updates, finalization, and cancellation.
    Supports multi-message handling, splitting long replies into per-bubble-sized
    messages (``max_bubble_chars``, configurable via CCC_TELEGRAM_MAX_BUBBLE_CHARS).
    """

    def __init__(self, bot: Bot, chat_id: int, user_id: int, *, settings: Any = None):
        runtime_config = config if settings is None else settings
        self.bot = bot
        self.chat_id = chat_id
        self.user_id = user_id
        self.drafts: List[DraftState] = []
        self.accumulated_text: str = ""
        self.tool_calls_text: str = ""  # Accumulated tool call display text
        self.min_chars = runtime_config.draft_update_min_chars
        self.min_interval = runtime_config.draft_update_interval
        # Max characters per Telegram message ("bubble"). Long replies overflow
        # into a new draft at this size during streaming so no single bubble is
        # overwhelming. Clamped to the Telegram hard limit as a safety bound.
        self.max_bubble_chars = max(
            200,
            min(
                int(getattr(runtime_config, "telegram_max_bubble_chars", 4000)),
                tg_md.TELEGRAM_LIMIT,
            ),
        )
        self.enable_tool_calls = getattr(
            runtime_config, "enable_streaming_tool_calls", False
        )
        self._finalized = False
        self._draft_seq = 0

    def _next_draft_id(self) -> str:
        self._draft_seq += 1
        return f"{self.user_id}-{int(time.time() * 1000)}-{self._draft_seq}"

    @staticmethod
    def _format_tool_call(name: str, input: dict) -> str:
        """Format tool call for display in Telegram"""
        # Extract key arguments for summary
        if name == "Bash" and "command" in input:
            summary = input["command"]
        elif name in ("Read", "Write", "Edit", "MultiEdit") and "file_path" in input:
            summary = input["file_path"]
        elif name == "Glob" and "pattern" in input:
            summary = input["pattern"]
        elif name == "Grep" and "pattern" in input:
            summary = input["pattern"]
        elif name == "WebFetch" and "url" in input:
            summary = input["url"]
        elif name == "WebSearch" and "query" in input:
            summary = input["query"]
        elif name == "Agent" and "subagent_type" in input:
            summary = input["subagent_type"]
        elif name == "Task" and "description" in input:
            summary = input["description"]
        elif name == "AskUserQuestion":
            # Extract question text if available
            if "questions" in input and input["questions"]:
                summary = input["questions"][0].get("question", "asking...")
            else:
                summary = "asking..."
        else:
            # Generic: show truncated input
            summary = str(input)[:80]

        return f"🛠️ **{name}**: `{summary}`\n"

    async def add_tool_call(self, name: str, input: dict) -> bool:
        """Add a tool call notification to the streaming output"""
        if self._finalized or not self.enable_tool_calls:
            return False

        tool_line = self._format_tool_call(name, input)
        self.tool_calls_text += tool_line

        # Rebuild full text: tool calls prefix + accumulated content
        full_text = self.tool_calls_text + self.accumulated_text

        # Create or update draft immediately (tool calls are important)
        if not self.drafts:
            await self.create_draft(full_text)
        else:
            current_draft = self.drafts[-1]
            await self.update_draft(current_draft, full_text)

        return True

    async def _retry_with_backoff(self, operation, max_retries=3):
        """Execute operation with exponential backoff on flood control errors."""
        for attempt in range(max_retries):
            try:
                return await operation()
            except RetryAfter as e:
                if attempt == max_retries - 1:
                    raise
                wait_time = (
                    float(e.retry_after) if hasattr(e, "retry_after") else (2**attempt)
                )
                logger.warning(
                    f"Rate limited, waiting {wait_time}s (retry {attempt + 1}/{max_retries})"
                )
                await asyncio.sleep(wait_time)

    @staticmethod
    def _extract_message_id(message: Any) -> Optional[int]:
        message_id = getattr(message, "message_id", None)
        return message_id if isinstance(message_id, int) else None

    @staticmethod
    def _is_not_modified_error(error: Exception) -> bool:
        return "message is not modified" in str(error).lower()

    async def create_draft(self, text: str) -> Optional[DraftState]:
        """Send initial draft message"""
        content = text or "..."
        try:
            sent_message = await self._retry_with_backoff(
                lambda: self.bot.send_message(
                    chat_id=self.chat_id,
                    text=content,
                )
            )
            message_id = self._extract_message_id(sent_message)
            if message_id is None:
                raise RuntimeError(
                    "send_message did not return a message with valid message_id"
                )

            draft = DraftState(
                message_id=message_id,
                text=text,
                last_update_time=time.time(),
                char_count_since_update=0,
                draft_id=None,
            )
            self.drafts.append(draft)
            logger.debug(
                f"Created draft message {draft.message_id} for user {self.user_id}"
            )
            return draft
        except TelegramError as e:
            logger.error(f"Failed to create draft message: {e}")
            return None
        except Exception as e:
            logger.error(f"Failed to create draft message: {e}")
            return None

    async def update_draft(self, draft: DraftState, new_text: str) -> bool:
        """Update existing draft with new text"""
        try:
            await self._retry_with_backoff(
                lambda: self.bot.edit_message_text(
                    chat_id=self.chat_id, message_id=draft.message_id, text=new_text
                )
            )
            draft.text = new_text
            draft.last_update_time = time.time()
            draft.char_count_since_update = 0
            logger.debug(f"Updated draft {draft.message_id} ({len(new_text)} chars)")
            return True
        except TelegramError as e:
            if self._is_not_modified_error(e):
                draft.text = new_text
                draft.last_update_time = time.time()
                draft.char_count_since_update = 0
                logger.debug(
                    f"Draft {draft.message_id} unchanged on update, treated as success"
                )
                return True
            logger.error(f"Failed to update draft {draft.message_id}: {e}")
            return False
        except Exception as e:
            # Match create_draft's resilience: a non-Telegram transport error
            # (OSError, etc.) during an edit must not propagate out through the
            # reader loop and abort message processing mid-stream. Treat it as a
            # failed update and carry on.
            logger.error(f"Failed to update draft {draft.message_id}: {e}")
            return False

    def should_update(self, draft: DraftState, new_char_count: int) -> bool:
        """Check if draft should be updated based on thresholds"""
        time_elapsed = time.time() - draft.last_update_time
        return new_char_count >= self.min_chars or time_elapsed >= self.min_interval

    async def _finalize_with_entities(self, draft: DraftState, chunks) -> bool:
        """Finalize using (text + entities) instead of a MarkdownV2 string.

        Returns True on success. Returns False if the primary edit fails, so the
        caller falls back to the MarkdownV2 path (the draft still holds the
        original plain text, so re-editing there does not duplicate content).
        No parse_mode is set when entities are used (they are mutually exclusive).
        """
        first_text, first_entities = chunks[0]

        async def _edit():
            return await self.bot.edit_message_text(
                chat_id=self.chat_id,
                message_id=draft.message_id,
                text=first_text,
                entities=first_entities or None,
            )

        try:
            await self._retry_with_backoff(_edit)
        except TelegramError as e:
            if self._is_not_modified_error(e):
                return True
            logger.warning(
                f"Entity finalize failed for draft {draft.message_id} ({e}); "
                "falling back to MarkdownV2"
            )
            return False

        # Overflow chunks as follow-up messages (entities, no parse_mode).
        for chunk_text, chunk_entities in chunks[1:]:
            try:
                await self._retry_with_backoff(
                    lambda t=chunk_text, ents=chunk_entities: self.bot.send_message(
                        chat_id=self.chat_id, text=t, entities=ents or None
                    )
                )
            except TelegramError as e:
                logger.warning(f"Entity overflow chunk send failed: {e}")
        logger.debug(f"Finalized draft {draft.message_id} via entities")
        return True

    async def finalize_draft(self, draft: DraftState) -> bool:
        """Convert draft to a regular message, rendering Markdown -> MarkdownV2.

        Live draft updates stay plain text (cheap, no parse risk); on finalize we
        upgrade to MarkdownV2 so tables render as aligned code blocks and special
        characters/formatting show correctly. Any parse/length edge case falls
        back to the original plain text — delivery is never lost.
        """
        # Convert to MarkdownV2, then split on entity-safe boundaries. MarkdownV2
        # escaping expands the text (~1.2x, more for tables/symbol-dense content),
        # so a sub-limit raw draft can exceed TELEGRAM_LIMIT once escaped. We used
        # to drop the whole draft to plain text in that case (all formatting lost);
        # instead we now upgrade the draft to the first chunk and send the overflow
        # as follow-up MarkdownV2 messages.
        # Optionally normalize layout for mobile readability before MarkdownV2
        # (opt-in via CCC_TELEGRAM_READABLE_RENDERER). Content-preserving and
        # fail-open; the plain fallback below still uses the original draft.text
        # so delivery is never affected.
        render_text = tg_readable.render_for_delivery(
            draft.text,
            enabled=getattr(config, "enable_readable_renderer", False),
            loose=getattr(config, "enable_loose_spacing", False),
            spacing=getattr(config, "spacing_lines", 1),
        )

        # Optional 'k/N' part markers on multi-chunk responses (opt-in via
        # CCC_TELEGRAM_PART_HEADERS). Reserve headroom in the split limit first so
        # a marker can never push a chunk past the Telegram limit. The same
        # reserve applies to both the entity path and the MarkdownV2 fallback so
        # markers behave consistently regardless of which renderer wins.
        part_headers = getattr(config, "enable_part_headers", False)
        split_limit = tg_md.TELEGRAM_LIMIT - (
            tg_readable.PART_HEADER_RESERVE if part_headers else 0
        )

        # Entity path (opt-in via CCC_TELEGRAM_ENTITY_RENDERER): send
        # (text + MessageEntity[]) without parse_mode, avoiding MarkdownV2 escape
        # failures. Any failure / unavailable API falls through to MarkdownV2.
        if getattr(config, "enable_entity_renderer", False):
            entity_chunks = tg_entities.to_entity_chunks(render_text, split_limit)
            streaming_header = self._streaming_part_header(draft)
            if entity_chunks and part_headers and len(entity_chunks) > 1:
                entity_chunks = tg_entities.apply_part_headers(entity_chunks)
            elif entity_chunks and streaming_header:
                entity_chunks = [
                    tg_entities.apply_single_part_header(
                        entity_chunks[0], streaming_header[0], streaming_header[1]
                    )
                ]
            if entity_chunks and await self._finalize_with_entities(
                draft, entity_chunks
            ):
                return True

        md2 = tg_md.to_markdownv2(render_text)
        parts = tg_md.split_markdownv2(md2, split_limit) if md2 is not None else None
        streaming_header = self._streaming_part_header(draft)
        if parts and part_headers and len(parts) > 1:
            parts = tg_readable.apply_part_headers(parts)
        elif parts and streaming_header:
            parts = [
                f"{tg_readable.part_marker(streaming_header[0], streaming_header[1])}\n{parts[0]}",
                *parts[1:],
            ]
        use_md2 = bool(parts)
        md2_applied = False

        async def _edit():
            nonlocal md2_applied
            if use_md2:
                try:
                    res = await self.bot.edit_message_text(
                        chat_id=self.chat_id,
                        message_id=draft.message_id,
                        text=parts[0],
                        parse_mode="MarkdownV2",
                    )
                    md2_applied = True
                    return res
                except BadRequest:
                    md2_applied = False  # parse edge case -> plain text below
            return await self.bot.edit_message_text(
                chat_id=self.chat_id, message_id=draft.message_id, text=draft.text
            )

        try:
            await self._retry_with_backoff(_edit)
        except TelegramError as e:
            if self._is_not_modified_error(e):
                logger.debug(f"Draft {draft.message_id} already up-to-date on finalize")
                return True
            logger.error(f"Failed to finalize draft {draft.message_id}: {e}")
            return False

        # Send overflow chunks as follow-up messages — only when the primary
        # MarkdownV2 edit actually applied (the plain fallback already carries the
        # full draft text, so emitting extras then would duplicate content).
        if md2_applied and parts and len(parts) > 1:
            for extra in parts[1:]:
                try:
                    await self._retry_with_backoff(
                        lambda t=extra: self.bot.send_message(
                            chat_id=self.chat_id, text=t, parse_mode="MarkdownV2"
                        )
                    )
                except TelegramError as e:
                    logger.warning(
                        f"Overflow chunk MarkdownV2 send failed ({e}); retrying plain"
                    )
                    try:
                        await self._retry_with_backoff(
                            lambda t=extra: self.bot.send_message(
                                chat_id=self.chat_id, text=t
                            )
                        )
                    except TelegramError as e2:
                        logger.error(f"Overflow chunk plain send failed: {e2}")

        logger.debug(f"Finalized draft {draft.message_id}")
        return True

    def _find_split_boundary(self, text: str, max_length: Optional[int] = None) -> int:
        """Find smart boundary for text splitting (paragraph > line > hard cut).

        ``max_length`` defaults to the configured per-bubble size. Avoids cutting
        through a fenced code block or a contiguous pipe table: if the chosen
        boundary lands inside such a block, back up to the block's start so each
        draft renders a whole table/code block instead of two broken halves.
        Backing up is floored at ``max_length // 2`` to avoid pathologically
        small chunks (a single huge block degrades gracefully).
        """
        if max_length is None:
            max_length = self.max_bubble_chars
        if len(text) <= max_length:
            return len(text)

        floor = max(1, max_length // 2)

        # Prefer paragraph boundaries across the useful back half of the chunk,
        # not only the final 200 characters. This makes the configured per-bubble
        # size a target ceiling while still producing readable semantic bubbles.
        para_idx = text.rfind("\n\n", floor, max_length)
        if para_idx >= floor:
            return self._avoid_block_split(text, para_idx + 2, max_length)

        # Then try a heading boundary so a new section starts a new bubble.
        heading_idx = text.rfind("\n#", floor, max_length)
        if heading_idx >= floor:
            return self._avoid_block_split(text, heading_idx + 1, max_length)

        # Try line boundary (single newline)
        line_idx = text.rfind("\n", floor, max_length)
        if line_idx >= floor:
            return self._avoid_block_split(text, line_idx + 1, max_length)

        # Hard cut at max_length
        return self._avoid_block_split(text, max_length, max_length)

    @staticmethod
    def _avoid_block_split(text: str, cut: int, max_length: int) -> int:
        """Pull *cut* back to a block boundary if it falls inside a code/table block."""
        floor = max(1, max_length // 2)
        prefix = text[:cut]

        # Inside a fenced code block? (odd number of ``` fences before the cut)
        if prefix.count("```") % 2 == 1:
            fence = prefix.rfind("```")
            line_start = prefix.rfind("\n", 0, fence) + 1  # 0 if no newline
            if line_start >= floor:
                return line_start

        # Inside a contiguous pipe table? The last emitted line is a table row
        # (contains '|') and the next line continues the table. Walk back over
        # consecutive table rows to the line before the block.
        lines = prefix.split("\n")
        # prefix ends with '\n' (cut is at a line boundary) -> last element ''.
        idx = len(lines) - 2 if lines and lines[-1] == "" else len(lines) - 1
        next_line = text[cut:].split("\n", 1)[0]
        if 0 <= idx < len(lines) and "|" in lines[idx] and "|" in next_line:
            back = cut
            j = idx
            while j >= 0 and "|" in lines[j]:
                back -= len(lines[j]) + 1  # +1 for the newline
                j -= 1
            back = max(back, 0)
            if back >= floor:
                return back

        return cut

    def _first_draft_prefix(self) -> str:
        """Return tool calls prefix if we're still on the first draft, else empty string."""
        return self.tool_calls_text if len(self.drafts) <= 1 else ""

    @staticmethod
    def _streaming_part_header(draft: DraftState) -> Optional[tuple[int, int]]:
        index = getattr(draft, "part_header_index", None)
        total = getattr(draft, "part_header_total", None)
        if isinstance(index, int) and isinstance(total, int) and total > 1:
            return index, total
        return None

    def _apply_streaming_part_headers(self) -> None:
        """Record k/N markers for multi-draft streaming responses.

        Per-chunk part headers inside ``finalize_draft`` only fire when a single
        finalized draft splits into multiple Telegram messages. Streaming mode
        can create several ~4K drafts first; each draft then finalizes as a
        single chunk, so no marker ever appears. Once ``finalize_all`` runs we
        know the total draft count and can mark each finalized draft in place.
        """
        if not getattr(config, "enable_part_headers", False):
            return
        total = len(self.drafts)
        if total <= 1:
            return
        for index, draft in enumerate(self.drafts, 1):
            draft.part_header_index = index
            draft.part_header_total = total

    async def handle_overflow(self) -> bool:
        """Handle the per-bubble boundary by finalizing the current draft and creating a new one."""
        if not self.drafts:
            return False

        current_draft = self.drafts[-1]
        split_point = self._find_split_boundary(self.accumulated_text)

        # Finalize current draft with text up to split point
        # Include tool_calls_text prefix only on the first draft
        prefix = self._first_draft_prefix()
        finalize_text = prefix + self.accumulated_text[:split_point]
        current_draft.text = finalize_text
        await self.finalize_draft(current_draft)

        # Create new draft with remaining text. Seed it only up to its own split
        # boundary so the initial send never exceeds the Telegram per-message
        # limit — the caller's overflow loop finalizes/splits the rest on the
        # next iteration. (When the remainder already fits, _find_split_boundary
        # returns its full length, so this is a no-op for the common case.)
        remaining_text = self.accumulated_text[split_point:]
        self.accumulated_text = remaining_text

        if remaining_text:
            seed_boundary = self._find_split_boundary(remaining_text)
            await self.create_draft(remaining_text[:seed_boundary])
            logger.debug(
                f"Created overflow draft, remaining {len(remaining_text)} chars"
            )

        return True

    async def update_if_needed(self, new_text_chunk: str) -> bool:
        """
        Main entry point: accumulate text and update draft if thresholds met.
        Handles overflow to new drafts when exceeding the per-bubble size
        (``max_bubble_chars``, configurable via CCC_TELEGRAM_MAX_BUBBLE_CHARS).

        A single chunk can already be hundreds–thousands of characters. We
        accumulate the whole chunk, split it across the per-bubble size into
        separate drafts, then push AT MOST ONE edit for the trailing draft
        (threshold-gated).

        The previous implementation sliced a large chunk into ``min_chars``
        pieces and issued one ``edit_message_text`` per slice — N sequential
        network round-trips against the *same* message (N = len/min_chars). Every
        intermediate edit was overwritten ~instantly (never seen by the user) and
        the rapid same-message edits routinely tripped Telegram's per-message
        edit flood limit, forcing RetryAfter backoff. That was pure added
        latency, and because the reader loop awaits this inline it also delayed
        the final ResultMessage. Collapsing to a single edit removes both costs.
        """
        if self._finalized:
            return False

        chunk_size = len(new_text_chunk)
        self.accumulated_text += new_text_chunk
        logger.debug(
            f"Received chunk: {chunk_size} chars, accumulated: {len(self.accumulated_text)} chars"
        )

        # The first draft must exist before handle_overflow (which finalizes the
        # *current* draft in place). Seed it with content only up to the first
        # split boundary so the initial send is never oversized.
        if not self.drafts:
            boundary = self._find_split_boundary(self.accumulated_text)
            seed = self._first_draft_prefix() + self.accumulated_text[:boundary]
            await self.create_draft(seed)
            # create_draft returns None and leaves drafts empty on a failed send.
            # Without a first draft, handle_overflow() below returns immediately
            # without consuming accumulated_text, so the overflow `while` would
            # spin forever with no await point and hang the whole event loop.
            # Bail this round instead; the next chunk retries the first send.
            if not self.drafts:
                return False

        # Split off complete drafts while the buffer exceeds the per-bubble size.
        while len(self.accumulated_text) >= self.max_bubble_chars:
            await self.handle_overflow()

        if not self.drafts:
            return True

        # One consolidated update for whatever remains (threshold-gated).
        current_draft = self.drafts[-1]
        display_text = self._first_draft_prefix() + self.accumulated_text
        chars_since_update = len(display_text) - len(current_draft.text)
        current_draft.char_count_since_update = chars_since_update

        logger.debug(
            f"Checking update: {chars_since_update} chars since last update, min_chars={self.min_chars}"
        )

        if chars_since_update and self.should_update(current_draft, chars_since_update):
            await self.update_draft(current_draft, display_text)
            return True

        return False

    async def finalize_all(self) -> bool:
        """Finalize all active draft messages"""
        if self._finalized:
            return False

        self._finalized = True

        # Update last draft with final accumulated text (include tool calls prefix for first draft)
        if self.drafts and self.accumulated_text:
            current_draft = self.drafts[-1]
            final_text = self._first_draft_prefix() + self.accumulated_text
            if current_draft.text != final_text:
                current_draft.text = final_text

        # Streaming overflow can produce multiple finalized drafts before the
        # final chunk arrives. Per-draft finalization cannot know the eventual
        # total, so apply k/N markers here when the whole response is known.
        self._apply_streaming_part_headers()

        # Finalize all drafts
        for draft in self.drafts:
            await self.finalize_draft(draft)

        logger.debug(f"Finalized {len(self.drafts)} draft(s) for user {self.user_id}")
        return True

    async def cancel(self) -> bool:
        """Delete all unfinished draft messages and clean up state"""
        if self._finalized:
            return False

        self._finalized = True

        # Delete all draft messages
        for draft in self.drafts:
            try:
                await self._retry_with_backoff(
                    lambda: self.bot.delete_message(
                        chat_id=self.chat_id, message_id=draft.message_id
                    )
                )
                logger.debug(f"Deleted draft {draft.message_id}")
            except TelegramError as e:
                logger.error(f"Failed to delete draft {draft.message_id}: {e}")

        self.drafts.clear()
        self.accumulated_text = ""
        self.tool_calls_text = ""
        logger.debug(f"Cancelled streaming for user {self.user_id}")
        return True
