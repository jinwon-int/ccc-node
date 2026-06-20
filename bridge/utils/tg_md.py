"""Markdown -> Telegram MarkdownV2 rendering.

Telegram's legacy ``Markdown`` parse mode is fragile: an unbalanced ``*``/``_``/
``` `` ``` or a stray special char raises ``BadRequest`` and the bridge then
drops to plain text, losing *all* formatting. It also has no table syntax, so
GFM pipe tables render as unreadable runs of ``|``.

This module renders standard Markdown into Telegram **MarkdownV2** via the
``telegramify-markdown`` library:
  * GFM pipe tables  -> aligned fixed-width code blocks (a real table on mobile)
  * special chars    -> escaped correctly (no more parse-error -> plain fallback)
  * headings/lists/code/links -> proper MarkdownV2 entities

Everything degrades gracefully: if the library is missing or a conversion
fails, the public helpers return ``None`` / a naive split so callers keep their
legacy ``wrap_markdown_tables`` + plain-text path. No hard dependency at import
time.
"""

import logging

logger = logging.getLogger(__name__)

TELEGRAM_LIMIT = 4096

_configured = False


def _ensure_config() -> None:
    """Strip telegramify's decorative heading emojis (📌/✏/📚...) once.

    We keep structure via bold headings; the emoji prefixes are visual noise in
    an ops/reporting context. Best-effort — never raises.
    """
    global _configured
    if _configured:
        return
    _configured = True
    try:
        from telegramify_markdown import config

        symbol = config.get_runtime_config().markdown_symbol
        for level in range(1, 7):
            setattr(symbol, f"heading_level_{level}", "")
    except Exception:  # noqa: BLE001 - config shape may change across versions
        pass


def available() -> bool:
    """Return True when telegramify-markdown can be imported."""
    try:
        import telegramify_markdown  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def to_markdownv2(text: str):
    """Convert markdown *text* to a Telegram MarkdownV2 string.

    Returns the converted string, or ``None`` if the library is unavailable or
    the conversion fails (so the caller can fall back to its legacy path).
    """
    if not text:
        return text
    try:
        import telegramify_markdown as tg
    except Exception:  # noqa: BLE001 - library optional
        return None
    try:
        _ensure_config()
        return tg.markdownify(text)
    except Exception as exc:  # noqa: BLE001 - any render failure -> caller fallback
        logger.warning("telegramify markdownify failed (%s); falling back", exc)
        return None


def utf16_len(text: str) -> int:
    """Telegram counts message length in UTF-16 code units, not Python chars."""
    try:
        import telegramify_markdown as tg
        return tg.utf16_len(text)
    except Exception:  # noqa: BLE001
        return len(text.encode("utf-16-le")) // 2


def split_markdownv2(text: str, limit: int = TELEGRAM_LIMIT):
    """Split already-converted MarkdownV2 on entity-safe boundaries.

    Falls back to a naive fixed-size split if the library is unavailable.
    """
    try:
        import telegramify_markdown as tg
        parts = tg.split_markdownv2(text, max_utf16_len=limit)
        return parts or [text]
    except Exception:  # noqa: BLE001
        if utf16_len(text) <= limit:
            return [text]
        return [text[i : i + limit] for i in range(0, len(text), limit)] or [text]
