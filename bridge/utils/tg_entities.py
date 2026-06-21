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


def apply_part_headers(chunks: List[EntityChunk]) -> List[EntityChunk]:
    """Prefix each entity chunk with a compact bold ``k/N`` continuation marker.

    Entity counterpart to ``tg_readable.apply_part_headers`` — that helper only
    runs on the MarkdownV2 fallback path, so without this the entity path emits
    multi-chunk responses with no part marker at all (GitHub issue #34, slice 5
    follow-up).

    Returns a new list. A single chunk (or empty input) is returned unchanged —
    a marker is only meaningful when a response spans multiple messages. Each
    chunk's existing MessageEntity offsets are shifted by the UTF-16 length of
    the prepended ``"k/N\\n"`` text, and a bold entity is added over the
    ``k/N`` digits so it renders identically to the MarkdownV2 ``*k/N*`` marker
    (the entity path sends without ``parse_mode``, so asterisks would otherwise
    appear as literal text).

    If ``telegram.MessageEntity`` is unavailable (pragma: no cover branch in
    ``available()``), the input is returned as a list copy so the caller can
    continue without raising.
    """
    chunks = list(chunks)
    if PTBMessageEntity is None:
        return chunks
    total = len(chunks)
    if total <= 1:
        return chunks
    out: List[EntityChunk] = []
    for index, (text, entities) in enumerate(chunks, 1):
        marker_text = f"{index}/{total}"
        prefix = f"{marker_text}\n"
        # ASCII prefix → UTF-16 code-unit length equals str length. Use the
        # telegramify_markdown helper when available to stay consistent with
        # how split_entities measures offsets.
        try:
            shift = _tm.utf16_len(prefix)
        except Exception:  # pragma: no cover - utf16_len only fails on bad input
            shift = len(prefix)
        marker_entity = PTBMessageEntity(
            type="bold", offset=0, length=len(marker_text)
        )
        shifted = [
            PTBMessageEntity(
                type=e.type,
                offset=e.offset + shift,
                length=e.length,
                url=getattr(e, "url", None),
                language=getattr(e, "language", None),
                custom_emoji_id=getattr(e, "custom_emoji_id", None),
            )
            for e in entities
        ]
        out.append((prefix + text, [marker_entity, *shifted]))
    return out
