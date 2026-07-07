"""Pure presentation helpers for the Telegram bridge.

These functions are intentionally side-effect free: they take plain inputs and
return text chunks or ``telegram`` keyboard markup objects, touching no bot
instance state. They were extracted from ``core/bot.py`` (the ``TelegramBot``
god object) so the rendering/keyboard logic can be unit-tested directly instead
of only through the full bot. ``TelegramBot`` keeps thin delegating methods, so
existing call sites and behavior are unchanged.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

# Matches numbered options like "1. foo", "2、bar", "3) baz" at line starts.
OPTION_RE = re.compile(r"^\s*(\d+)[.、)）]\s*(.+)", re.MULTILINE)


def split_text(text: str, limit: int = 4000) -> List[str]:
    """Split text into chunks no longer than limit, breaking at paragraph or line boundaries."""
    if len(text) <= limit:
        return [text]
    chunks: List[str] = []
    remaining = text
    while len(remaining) > limit:
        # Try to split at a paragraph boundary (double newline)
        cut = remaining.rfind("\n\n", 0, limit)
        if cut == -1:
            # Fall back to single newline
            cut = remaining.rfind("\n", 0, limit)
        if cut == -1:
            # Hard cut at limit
            cut = limit
        else:
            cut += 1  # include the newline in the current chunk
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


def format_relative_time(timestamp: str) -> str:
    """Format timestamp as relative time.

    Returns:
        - "Just now" for < 1 minute
        - "X minutes ago" for < 1 hour
        - "X hours ago" for < 24 hours (today)
        - "Yesterday" for yesterday
        - "X days ago" for 2-3 days ago
        - "MM-DD" for > 3 days ago
    """
    if not timestamp:
        return ""

    try:
        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        # A timestamp with no "Z" and no explicit offset parses as tz-naive;
        # subtracting it from a tz-aware `now` raises TypeError, so every such
        # button would silently fall back to a raw date. Assume UTC for naive
        # inputs so relative formatting keeps working.
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        diff = now - dt

        total_seconds = diff.total_seconds()

        # Less than 1 minute
        if total_seconds < 60:
            return "Just now"

        # Less than 1 hour
        if total_seconds < 3600:
            minutes = int(total_seconds / 60)
            return f"{minutes}m ago"

        # Less than 24 hours (today)
        if total_seconds < 86400:
            hours = int(total_seconds / 3600)
            return f"{hours}h ago"

        # Calculate days
        days = int(total_seconds / 86400)

        # Yesterday
        if days == 1:
            return "Yesterday"

        # 2-3 days ago
        if days <= 3:
            return f"{days}d ago"

        # More than 3 days - show date
        return dt.strftime("%m-%d")

    except Exception:
        return timestamp[:10] if len(timestamp) >= 10 else ""


def extract_options(text: str) -> List[str]:
    """Extract numbered options from text like '1. xxx\n2. xxx'."""
    matches = OPTION_RE.findall(text)
    if len(matches) < 2:
        return []
    # Verify consecutive numbering starting from 1
    nums = [int(m[0]) for m in matches]
    if nums != list(range(1, len(nums) + 1)):
        return []
    return [m[1].strip() for m in matches]


def build_option_keyboard(options: List[str]) -> Optional[InlineKeyboardMarkup]:
    """Build inline keyboard from extracted options."""
    if not options:
        return None
    buttons = []
    for i, opt in enumerate(options, 1):
        # callback_data max 64 bytes; truncate label if needed
        label = f"{i}. {opt}"
        cb_data = f"opt:{label}"
        if len(cb_data.encode("utf-8")) > 64:
            cb_data = f"opt:{i}"
        buttons.append([InlineKeyboardButton(label, callback_data=cb_data)])
    return InlineKeyboardMarkup(buttons)


def build_history_keyboard(
    messages: List[Dict[str, Any]], page: int = 0, page_size: int = 10
) -> InlineKeyboardMarkup:
    """Build inline keyboard for message history selection.

    Args:
        messages: List of user message dicts with index, timestamp, role, content (newest first)
        page: Current page number (0-indexed)
        page_size: Number of messages per page

    Returns:
        InlineKeyboardMarkup with message buttons and pagination controls
    """
    start_idx = page * page_size
    end_idx = start_idx + page_size
    page_messages = messages[start_idx:end_idx]

    buttons = []
    for offset, msg in enumerate(page_messages):
        # Format relative time
        timestamp = msg.get("timestamp", "")
        time_str = format_relative_time(timestamp)

        # Truncate content preview
        content = msg.get("content", "")
        preview = content[:40] + "..." if len(content) > 40 else content
        preview = preview.replace("\n", " ")

        # Format button label with relative time
        label = f"💬 {time_str} {preview}"

        # Callback data: revert:select:{index}. Fall back to the computed
        # absolute position when a record lacks an explicit "index" so a single
        # malformed entry can't crash the whole keyboard (leaving /revert blank).
        index = msg.get("index", start_idx + offset)
        cb_data = f"revert:select:{index}"
        buttons.append([InlineKeyboardButton(label, callback_data=cb_data)])

    # Add pagination buttons if needed
    pagination_row = []
    total_pages = (len(messages) + page_size - 1) // page_size

    if page > 0:
        pagination_row.append(
            InlineKeyboardButton("◀️ Previous", callback_data=f"revert:page:{page - 1}")
        )
    if page < total_pages - 1:
        pagination_row.append(
            InlineKeyboardButton("Next ▶️", callback_data=f"revert:page:{page + 1}")
        )

    if pagination_row:
        buttons.append(pagination_row)

    return InlineKeyboardMarkup(buttons)


def build_revert_mode_keyboard(msg_index: int) -> InlineKeyboardMarkup:
    """Build inline keyboard for revert mode selection.

    Args:
        msg_index: Index of the selected message in JSONL file

    Returns:
        InlineKeyboardMarkup with 5 revert mode options
    """
    buttons = [
        [
            InlineKeyboardButton(
                "🔄 Restore code and conversation",
                callback_data=f"revert:mode:{msg_index}:full",
            )
        ],
        [
            InlineKeyboardButton(
                "💬 Restore conversation only",
                callback_data=f"revert:mode:{msg_index}:conv",
            )
        ],
        [
            InlineKeyboardButton(
                "📝 Restore code only",
                callback_data=f"revert:mode:{msg_index}:code",
            )
        ],
        [
            InlineKeyboardButton(
                "📋 Summarize from here",
                callback_data=f"revert:mode:{msg_index}:summary",
            )
        ],
        [
            InlineKeyboardButton(
                "❌ Cancel", callback_data=f"revert:mode:{msg_index}:cancel"
            )
        ],
    ]
    return InlineKeyboardMarkup(buttons)
