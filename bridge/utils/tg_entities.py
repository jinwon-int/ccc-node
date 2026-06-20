"""Entity-based Telegram output (GitHub issue #34, slice 3).

Instead of sending Markdown as a MarkdownV2 *string* — where a single bad escape
can make Telegram reject the whole message — convert it to
``(plain_text, MessageEntity[])`` via ``telegramify-markdown``'s entity API and
send with ``entities=`` and **no** ``parse_mode``. This sidesteps MarkdownV2
escaping failures for bold/italic/code/links.

``telegramify_markdown.convert()`` returns its *own* ``MessageEntity`` objects,
which are not ``telegram.MessageEntity`` instances, so we map them. Chunking uses
``split_entities`` (UTF-16 aware) to keep each chunk within Telegram's limit.

Fail-open: if the library/API is unavailable or anything fails, returns ``None``
and the caller keeps the existing MarkdownV2 path. Never raises.
"""

from __future__ import annotations

import logging
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

try:  # telegramify-markdown is the same dependency tg_md uses
    import telegramify_markdown as _tm
except Exception:  # pragma: no cover - dependency missing
    _tm = None

try:
    from telegram import MessageEntity as PTBMessageEntity
except Exception:  # pragma: no cover - telegram always present in the bridge
    PTBMessageEntity = None

TELEGRAM_LIMIT = 4096

# (chunk_text, [telegram.MessageEntity, ...])
EntityChunk = Tuple[str, List["PTBMessageEntity"]]


def available() -> bool:
    """True if entity rendering can be attempted."""
    return (
        _tm is not None
        and PTBMessageEntity is not None
        and hasattr(_tm, "convert")
        and hasattr(_tm, "split_entities")
    )


def _to_ptb_entity(e) -> "PTBMessageEntity":
    """Map a telegramify_markdown MessageEntity to telegram.MessageEntity."""
    return PTBMessageEntity(
        type=e.type,
        offset=e.offset,
        length=e.length,
        url=getattr(e, "url", None),
        language=getattr(e, "language", None),
        custom_emoji_id=getattr(e, "custom_emoji_id", None),
    )


def to_entity_chunks(
    text: str, limit: int = TELEGRAM_LIMIT
) -> Optional[List[EntityChunk]]:
    """Convert *text* to entity-based chunks for entity-mode sending.

    Returns a list of ``(chunk_text, [telegram.MessageEntity])`` each within
    *limit* UTF-16 units, or ``None`` if entity rendering is unavailable or
    fails (so the caller can fall back to the MarkdownV2 path).
    """
    if not available() or not text:
        return None
    try:
        plain, entities = _tm.convert(text)
        pieces = _tm.split_entities(plain, entities, limit)
        result: List[EntityChunk] = [
            (chunk_text, [_to_ptb_entity(e) for e in chunk_entities])
            for chunk_text, chunk_entities in pieces
        ]
        return result or None
    except Exception:
        logger.warning(
            "tg_entities.to_entity_chunks failed; falling back to MarkdownV2",
            exc_info=True,
        )
        return None
