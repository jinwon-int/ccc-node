"""Bounded inbound Bot API rich text, including PTB's unknown-field fallback.

Only visible text is admitted. Unsupported blocks reject the whole input rather
than quietly turning a partial document into an instruction. No URLs are fetched.
Source contract: https://core.telegram.org/bots/api#richmessage (2026-09-11).
"""
from collections.abc import Mapping
from typing import Any

from telegram.ext import filters

MAX_NODES = 4096
MAX_DEPTH = 24
MAX_TEXT = 32768


class RichTextError(ValueError):
    """Unsupported, malformed or oversized rich input; never contains its body."""


def rich_payload(message: Any) -> Any:
    value = getattr(message, "rich_message", None)
    if value is not None:
        return value.to_dict() if callable(getattr(value, "to_dict", None)) else value
    extra = getattr(message, "api_kwargs", None)
    return extra.get("rich_message") if isinstance(extra, Mapping) else None


class InboundRichMessageFilter(filters.MessageFilter):
    def filter(self, message: Any) -> bool:
        # Do not parse the body before the ordinary access check.
        extra = getattr(message, "api_kwargs", None)
        return getattr(message, "rich_message", None) is not None or (
            isinstance(extra, Mapping) and "rich_message" in extra
        )


RICH_MESSAGE = InboundRichMessageFilter(name="InboundRichMessage")


def _budget(value: Any, depth: int = 0, counts: list[int] | None = None) -> None:
    if counts is None:
        counts = [0, 0]
    counts[0] += 1
    if depth > MAX_DEPTH or counts[0] > MAX_NODES:
        raise RichTextError("rich_structure_limit")
    if isinstance(value, str):
        counts[1] += len(value)
        if counts[1] > MAX_TEXT * 2:
            raise RichTextError("rich_text_limit")
    elif isinstance(value, dict):
        if len(value) > MAX_NODES:
            raise RichTextError("rich_structure_limit")
        for key, item in value.items():
            _budget(key, depth + 1, counts)
            _budget(item, depth + 1, counts)
    elif isinstance(value, list):
        if len(value) > MAX_NODES:
            raise RichTextError("rich_structure_limit")
        for item in value:
            _budget(item, depth + 1, counts)
    elif value is not None and type(value) not in (bool, int, float):
        raise RichTextError("rich_invalid_value")


_TEXT_WRAPPERS = frozenset({
    "bold", "italic", "underline", "strikethrough", "spoiler", "code", "marked",
    "subscript", "superscript", "text_mention", "custom_emoji", "url", "email_address",
    "phone_number", "bank_card_number", "mention", "hashtag", "cashtag", "bot_command",
    "anchor", "anchor_link", "reference", "reference_link", "date_time",
})


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_text(item) for item in value)
    if not isinstance(value, dict):
        raise RichTextError("rich_invalid_text")
    kind = value.get("type")
    if not isinstance(kind, str):
        raise RichTextError("rich_invalid_text_type")
    if kind == "custom_emoji":
        alternative = value.get("alternative_text")
        if not isinstance(alternative, str) or not alternative:
            raise RichTextError("rich_invalid_emoji")
        return alternative
    if kind == "mathematical_expression":
        expression = value.get("expression")
        if not isinstance(expression, str):
            raise RichTextError("rich_invalid_expression")
        return expression
    if kind not in _TEXT_WRAPPERS or "text" not in value:
        raise RichTextError("rich_unsupported_text")
    text = _text(value["text"])
    if kind == "url":
        url = value.get("url")
        if not isinstance(url, str):
            raise RichTextError("rich_invalid_url")
        return text if text == url else f"{text} ({url})"
    return text


def _blocks(value: Any) -> str:
    if not isinstance(value, list):
        raise RichTextError("rich_invalid_blocks")
    return "\n\n".join(_block(block) for block in value)


def _list(value: Any) -> str:
    if not isinstance(value, list):
        raise RichTextError("rich_invalid_list")
    lines = []
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("label"), str):
            raise RichTextError("rich_invalid_list_item")
        label = item["label"] or "•"
        if item.get("has_checkbox") is True:
            label = "[x]" if item.get("is_checked") is True else "[ ]"
        lines.append(f"{label} {_blocks(item.get('blocks'))}")
    return "\n".join(lines)


def _table(value: Any) -> str:
    if not isinstance(value, list):
        raise RichTextError("rich_invalid_table")
    lines = []
    for row in value:
        if not isinstance(row, list) or any(not isinstance(cell, dict) for cell in row):
            raise RichTextError("rich_invalid_table_row")
        lines.append(" | ".join(_text(cell.get("text", "")) for cell in row))
    return "\n".join(lines)


def _block(block: Any) -> str:
    if not isinstance(block, dict):
        raise RichTextError("rich_invalid_block")
    kind = block.get("type")
    if not isinstance(kind, str):
        raise RichTextError("rich_invalid_block_type")
    if kind in {"paragraph", "heading", "pre", "footer", "pullquote"}:
        text = _text(block.get("text"))
        return text + ("\n" + _text(block["credit"]) if "credit" in block else "")
    if kind == "list":
        return _list(block.get("items"))
    if kind == "table":
        caption = _text(block.get("caption", ""))
        return (caption + "\n" if caption else "") + _table(block.get("cells"))
    if kind in {"blockquote", "details"}:
        heading = _text(block.get("summary", ""))
        text = (heading + "\n" if heading else "") + _blocks(block.get("blocks"))
        return text + ("\n" + _text(block["credit"]) if "credit" in block else "")
    if kind == "divider":
        return "---"
    if kind == "mathematical_expression":
        return _text(block)
    # Media/buttons/unknown types need separate support; do not omit them.
    raise RichTextError("rich_unsupported_block")


def inbound_message_text(message: Any) -> str:
    plain = getattr(message, "text", None)
    if plain:
        return plain
    payload = rich_payload(message)
    if payload is None:
        return ""
    _budget(payload)
    if not isinstance(payload, dict):
        raise RichTextError("rich_invalid_message")
    result = _blocks(payload.get("blocks"))
    if not result.strip() or len(result) > MAX_TEXT:
        raise RichTextError("rich_empty_or_oversize")
    return result
