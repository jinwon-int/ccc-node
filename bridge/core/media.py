"""Pure voice / image helper functions for the Telegram bridge.

Side-effect-free utilities extracted from ``core/bot.py``: filename and
extension resolution, inbound-image selection, prompt/url construction, and the
voice reply-mode / delivery-strategy heuristics. They touch no bot instance
state (no network, no config beyond what is passed in), so they can be unit
tested directly. ``TelegramBot`` keeps thin delegating methods, leaving call
sites and behavior unchanged.
"""

from __future__ import annotations

import platform
import re
import time
from pathlib import Path as FilePath
from typing import Any, Optional, Tuple

# Voice delivery thresholds (previously TelegramBot class constants).
VOICE_TEXT_CHAR_THRESHOLD = 300
VOICE_LONG_HANZI_THRESHOLD = 1000
VOICE_LONG_ENGLISH_WORD_THRESHOLD = 1000


def resolve_voice_extension(mime_type: Optional[str]) -> str:
    if not mime_type:
        return "ogg"
    normalized = mime_type.lower()
    if "amr" in normalized:
        return "amr"
    if "mp3" in normalized or "mpeg" in normalized:
        return "mp3"
    if "wav" in normalized:
        return "wav"
    if "m4a" in normalized or "mp4" in normalized:
        return "m4a"
    return "ogg"


def build_voice_file_name(user_id: int, extension: str) -> str:
    timestamp_ms = int(time.time() * 1000)
    return f"{user_id}_{timestamp_ms}.{extension}"


def is_macos() -> bool:
    return platform.system() == "Darwin"


def count_hanzi(text: str) -> int:
    return len(re.findall(r"[一-鿿]", text))


def count_english_words(text: str) -> int:
    return len(re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", text))


def normalize_reply_mode(mode: Optional[str]) -> str:
    normalized = str(mode or "text").strip().lower()
    if normalized not in {"text", "voice"}:
        return "text"
    return normalized


def resolve_next_reply_mode(message_source: str, *, is_macos: bool) -> str:
    """Next reply mode: voice only for inbound voice on macOS, else text.

    ``is_macos`` is passed in (rather than read from the platform here) so the
    caller's gating decision stays the single source of truth and remains
    patchable in tests.
    """
    if not is_macos:
        return "text"
    if message_source == "voice":
        return "voice"
    return "text"


def voice_delivery_strategy(content: str) -> str:
    """Pick how to deliver a voice reply based on content length/script."""
    hanzi_count = count_hanzi(content)
    english_word_count = count_english_words(content)
    if (
        hanzi_count > VOICE_LONG_HANZI_THRESHOLD
        or english_word_count > VOICE_LONG_ENGLISH_WORD_THRESHOLD
    ):
        return "text_only"
    if len(content) > VOICE_TEXT_CHAR_THRESHOLD:
        return "voice_and_text"
    return "voice_only"


def redact_telegram_file_url(url: str) -> str:
    return re.sub(r"/bot[^/]+/", "/bot***REDACTED***/", url)


def resolve_image_extension(mime_type: Optional[str], file_name: Optional[str] = None) -> str:
    if file_name:
        suffix = FilePath(file_name).suffix.lower().lstrip(".")
        if suffix in {"jpg", "jpeg", "png", "webp", "gif", "bmp", "tif", "tiff"}:
            return "jpg" if suffix == "jpeg" else suffix
    mime = (mime_type or "").lower().split(";", 1)[0].strip()
    return {
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
        "image/gif": "gif",
        "image/bmp": "bmp",
        "image/tiff": "tiff",
    }.get(mime, "jpg")


def build_image_file_name(user_id: int, extension: str) -> str:
    safe_ext = re.sub(r"[^a-z0-9]", "", extension.lower()) or "jpg"
    return f"image_{user_id}_{int(time.time() * 1000)}.{safe_ext}"


def select_inbound_image(message: Any) -> Tuple[Optional[Any], str]:
    """Return (best image object, kind) for an inbound Telegram message.

    Prefers the largest photo size; falls back to an image/* document; else
    (None, "none").
    """
    photos = list(getattr(message, "photo", None) or [])
    if photos:
        def score(photo: Any) -> int:
            file_size = int(getattr(photo, "file_size", 0) or 0)
            pixels = int(getattr(photo, "width", 0) or 0) * int(getattr(photo, "height", 0) or 0)
            return max(file_size, pixels)

        return max(photos, key=score), "photo"

    document = getattr(message, "document", None)
    mime_type = str(getattr(document, "mime_type", "") or "").lower()
    if document is not None and mime_type.startswith("image/"):
        return document, "document"
    return None, "none"


def build_image_prompt(image_path: FilePath, caption: str) -> str:
    caption = (caption or "").strip()
    prompt = (
        "The user sent an inbound Telegram image. Analyze the image and answer the user's request.\n\n"
        f"Local image path: {image_path}\n"
    )
    if caption:
        prompt += f"Caption / user instruction: {caption}\n"
    else:
        prompt += "Caption / user instruction: Please describe what is in the image and mention any visible text.\n"
    prompt += (
        "If the current Claude Code runtime cannot directly inspect image files, say so clearly "
        "and explain what file was received instead of silently ignoring the image."
    )
    return prompt
