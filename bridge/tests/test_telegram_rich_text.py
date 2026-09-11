"""Actual PTB parsing/routing and delivery regressions for rich-only input."""
import asyncio
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import Update
from telegram.ext import MessageHandler, filters

from telegram_bot.core.telegram_rich_text import (
    MAX_DEPTH, MAX_NODES, MAX_TEXT, RICH_MESSAGE, RichTextError, inbound_message_text,
)
from telegram_bot.core.project_chat_state import PersistentFollowupQueue
from test_bot_delivery_paths import DeliveryHarness


def rich_update(blocks):
    return Update.de_json({
        "update_id": 200,
        "message": {"message_id": 100, "date": 1,
                    "chat": {"id": 10, "type": "private"},
                    "from": {"id": 1, "is_bot": False, "first_name": "Test"},
                    "rich_message": {"blocks": blocks}},
    }, None)


def paragraph(text):
    return {"type": "paragraph", "text": text}


def test_real_ptb_unknown_field_survives_roundtrip_and_filter():
    update = rich_update([paragraph(["안녕 ", {"type": "bold", "text": "서서"}]),
                          {"type": "list", "items": [
                              {"label": "1.", "blocks": [paragraph("첫째")]},
                              {"label": "2.", "blocks": [paragraph("둘째")]}]}])
    assert update.message.text is None
    assert not filters.TEXT.filter(update.message)  # original registration lost it
    handler = MessageHandler((filters.TEXT | RICH_MESSAGE) & ~filters.COMMAND, AsyncMock())
    assert handler.check_update(update)
    restored = Update.de_json(json.loads(update.to_json()), None)
    assert inbound_message_text(restored.message) == "안녕 서서\n\n1. 첫째\n2. 둘째"
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "queue.json"
        queue = PersistentFollowupQueue(path, per_chat_cap=4)
        item, _ = queue.enqueue(conversation_key="test", handler="text",
                                update_json=update.to_json(), enqueued_at=1)
        assert item is not None
        reopened = PersistentFollowupQueue(path, per_chat_cap=4)
        reopened.initialize()
        saved = reopened.peek("test")
        assert saved is not None
        replay = Update.de_json(json.loads(saved.update_json), None)
        assert inbound_message_text(replay.message) == inbound_message_text(update.message)
        assert "rich_message" in path.read_text()


def test_nested_text_tables_details_and_url_destination():
    update = rich_update([
        {"type": "details", "summary": "접힌 내용", "blocks": [
            {"type": "blockquote", "credit": "작성자", "blocks": [
                paragraph({"type": "url", "text": "문서", "url": "https://example.org/doc"})]}]},
        {"type": "table", "caption": "표", "cells": [[{"text": "A"}, {"text": "B"}],
                                                       [{"text": "1"}, {"text": "2"}]]},
        {"type": "pre", "text": "  $HOME\n    print('x')\n", "language": "python"},
        {"type": "list", "items": [{"label": "", "has_checkbox": True, "is_checked": True,
                                     "blocks": [paragraph("완료")]}]},
    ])
    text = inbound_message_text(update.message)
    assert "문서 (https://example.org/doc)" in text
    assert "접힌 내용\n" in text and "작성자" in text
    assert "표\nA | B\n1 | 2" in text
    assert "  $HOME\n    print('x')\n" in text
    assert "[x] 완료" in text


@pytest.mark.parametrize("blocks", [
    [paragraph("실행하면 안 됨"), {"type": "photo", "photo": []}],
    [paragraph("실행하면 안 됨"), {"type": "unknown", "text": "unknown"}],
    [paragraph({"type": "button", "text": "승인"})],
    [paragraph({"type": "bold", "text": 1})],
    [{"type": "list", "items": [{}]}],
    [{"type": "table", "cells": [1]}],
    [{"type": "details", "blocks": "bad"}],
    [paragraph("x" * (MAX_TEXT + 1))],
    [],
])
def test_unsupported_or_invalid_never_returns_partial_instruction(blocks):
    with pytest.raises(RichTextError):
        inbound_message_text(rich_update(blocks).message)


def test_bounded_deep_wide_and_cyclic_input():
    nested = "x"
    for _ in range(MAX_DEPTH + 1):
        nested = {"type": "bold", "text": nested}
    cases = [{"blocks": [paragraph(nested)]}, {"blocks": [paragraph("")] * MAX_NODES}]
    cyclic = {}
    cyclic["blocks"] = [cyclic]
    cases.append(cyclic)
    for payload in cases:
        with pytest.raises(RichTextError):
            inbound_message_text(SimpleNamespace(text=None, api_kwargs={"rich_message": payload}))


def test_plain_text_and_nontext_unchanged():
    assert inbound_message_text(SimpleNamespace(text="  original\n", api_kwargs={})) == "  original\n"
    assert inbound_message_text(SimpleNamespace(text=None, api_kwargs={})) == ""
    assert not RICH_MESSAGE.filter(SimpleNamespace(text=None, api_kwargs={}))


def test_real_delivery_and_access_gate():
    with tempfile.TemporaryDirectory() as tmp:
        bot = DeliveryHarness(tmp)
        update = rich_update([paragraph("읽어줘"), {"type": "list", "items": [
            {"label": "•", "blocks": [paragraph("이 항목도")]}]}])
        asyncio.run(bot._handle_text_message(update, None))
        assert bot.processed_texts == ["읽어줘\n\n• 이 항목도"]
        bot.access_granted = False
        asyncio.run(bot._handle_text_message(rich_update([{"type": "unknown"}]), None))
        assert len(bot.processed_texts) == 1


def test_invalid_rich_replies_without_processing_or_approval():
    with tempfile.TemporaryDirectory() as tmp:
        bot = DeliveryHarness(tmp)
        bot._resolve_codex_approval_text = AsyncMock()
        reply = AsyncMock()
        update = SimpleNamespace(message=SimpleNamespace(
            text=None, api_kwargs={"rich_message": {"blocks": [paragraph("yes"), {"type": "unknown"}]}},
            reply_text=reply))
        asyncio.run(bot._handle_text_message(update, None))
        assert not bot.processed_texts
        bot._resolve_codex_approval_text.assert_not_awaited()
        reply.assert_awaited_once()


@pytest.mark.parametrize("kind", [[], {}, 1, None])
def test_malformed_discriminator_has_categorical_error(kind):
    for block in [{"type": kind}, paragraph({"type": kind, "text": "x"})]:
        with pytest.raises(RichTextError):
            inbound_message_text(rich_update([block]).message)


def test_official_custom_emoji_and_datetime_keep_visible_text():
    update = rich_update([paragraph([
        {"type": "custom_emoji", "custom_emoji_id": "123", "alternative_text": "🙂"},
        " ", {"type": "date_time", "text": "오늘", "unix_time": 1, "date_time_format": "d"},
    ])])
    assert inbound_message_text(update.message) == "🙂 오늘"
