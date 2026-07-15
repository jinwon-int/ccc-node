# ruff: noqa: E402
# mypy: disable-error-code=attr-defined

import unittest
from types import SimpleNamespace
from pathlib import Path
import sys
import types
from telegram.error import TelegramError

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

config_module = types.ModuleType("telegram_bot.utils.config")
config_module.config = SimpleNamespace(
    draft_update_min_chars=20,
    draft_update_interval=0.1,
    enable_streaming_tool_calls=False,
)
sys.modules["telegram_bot.utils.config"] = config_module

from telegram_bot.core.streaming import StreamingMessageHandler
from telegram_bot.utils import tg_entities

_ENTITY_OK = tg_entities.available()


class _BotWithDraftId:
    def __init__(self):
        self.calls = []

    async def send_message(self, *, chat_id, text):
        self.calls.append(("send_message", chat_id, text))
        return SimpleNamespace(message_id=101)


class _BotDraftSignatureMismatch:
    def __init__(self):
        self.calls = []

    # Intentionally no draft_id parameter
    async def send_message_draft(self, *, chat_id, text):
        self.calls.append(("send_message_draft", chat_id, text))
        return SimpleNamespace(message_id=999)

    async def send_message(self, *, chat_id, text):
        self.calls.append(("send_message", chat_id, text))
        return SimpleNamespace(message_id=202)


class _BotDraftReturnsBool:
    def __init__(self):
        self.calls = []

    async def send_message_draft(self, *, chat_id, draft_id, text):
        self.calls.append(("send_message_draft", chat_id, draft_id, text))
        return True

    async def send_message(self, *, chat_id, text):
        self.calls.append(("send_message", chat_id, text))
        return SimpleNamespace(message_id=303)


class _BotEditNotModified:
    # parse_mode mirrors the real telegram Bot signature (finalize_draft now
    # passes parse_mode="MarkdownV2" on the upgrade attempt).
    async def edit_message_text(self, *, chat_id, message_id, text, parse_mode=None):
        raise TelegramError(
            "Message is not modified: specified new message content and reply markup are exactly the same as a current content and reply markup of the message"
        )


class _BotRecorder:
    def __init__(self):
        self.calls = []

    async def send_message(self, *, chat_id, text, parse_mode=None):
        self.calls.append(("send_message", chat_id, text))
        return SimpleNamespace(message_id=404)

    async def edit_message_text(self, *, chat_id, message_id, text, parse_mode=None):
        self.calls.append(("edit_message_text", chat_id, message_id, text))
        return True


class _BotCapture:
    """Records edit/send calls with their parse_mode for finalize-split tests."""

    def __init__(self):
        self.edits = []  # (message_id, text, parse_mode)
        self.sends = []  # (text, parse_mode)
        self._next_id = 500

    async def edit_message_text(self, *, chat_id, message_id, text, parse_mode=None):
        self.edits.append((message_id, text, parse_mode))
        return SimpleNamespace(message_id=message_id)

    async def send_message(self, *, chat_id, text, parse_mode=None):
        self._next_id += 1
        self.sends.append((text, parse_mode))
        return SimpleNamespace(message_id=self._next_id)


class StreamingMessageHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_finalize_segment_resets_draft_for_next_message(self):
        bot = _BotCapture()
        handler = StreamingMessageHandler(bot=bot, chat_id=42, user_id=7)

        await handler.update_if_needed("Checking now.")
        delivered = await handler.finalize_segment()
        await handler.update_if_needed("Final answer.")
        await handler.finalize_all()

        self.assertTrue(delivered)
        self.assertEqual([text for text, _ in bot.sends], ["Checking now.", "Final answer."])

    async def test_create_draft_uses_draft_api_with_draft_id(self):
        bot = _BotWithDraftId()
        handler = StreamingMessageHandler(bot=bot, chat_id=42, user_id=7)

        draft = await handler.create_draft("hello")

        self.assertIsNotNone(draft)
        self.assertEqual(draft.message_id, 101)
        self.assertIsNone(draft.draft_id)
        self.assertEqual(len(bot.calls), 1)
        self.assertEqual(bot.calls[0][0], "send_message")

    async def test_create_draft_falls_back_to_send_message_on_signature_mismatch(self):
        bot = _BotDraftSignatureMismatch()
        handler = StreamingMessageHandler(bot=bot, chat_id=42, user_id=7)

        draft = await handler.create_draft("hello")

        self.assertIsNotNone(draft)
        self.assertEqual(draft.message_id, 202)
        self.assertIsNone(draft.draft_id)
        self.assertEqual(len(bot.calls), 1)
        self.assertEqual(bot.calls[0][0], "send_message")

    async def test_create_draft_uses_send_message_even_if_draft_api_exists(self):
        bot = _BotDraftReturnsBool()
        handler = StreamingMessageHandler(bot=bot, chat_id=42, user_id=7)

        draft = await handler.create_draft("hello")

        self.assertIsNotNone(draft)
        self.assertEqual(draft.message_id, 303)
        self.assertIsNone(draft.draft_id)
        self.assertEqual(len(bot.calls), 1)
        self.assertEqual(bot.calls[0][0], "send_message")

    async def test_update_draft_treats_not_modified_as_success(self):
        bot = _BotEditNotModified()
        handler = StreamingMessageHandler(bot=bot, chat_id=42, user_id=7)
        draft = SimpleNamespace(
            message_id=992,
            text="old",
            last_update_time=0.0,
            char_count_since_update=10,
        )

        ok = await handler.update_draft(draft, "same")

        self.assertTrue(ok)
        self.assertEqual(draft.text, "same")
        self.assertEqual(draft.char_count_since_update, 0)

    async def test_finalize_draft_treats_not_modified_as_success(self):
        bot = _BotEditNotModified()
        handler = StreamingMessageHandler(bot=bot, chat_id=42, user_id=7)
        draft = SimpleNamespace(message_id=992, text="same")

        ok = await handler.finalize_draft(draft)

        self.assertTrue(ok)

    async def test_add_tool_call_is_disabled_by_default(self):
        bot = _BotRecorder()
        handler = StreamingMessageHandler(bot=bot, chat_id=42, user_id=7)

        ok = await handler.add_tool_call("Read", {"file_path": "/tmp/a.txt"})

        self.assertFalse(ok)
        self.assertEqual(handler.tool_calls_text, "")
        self.assertEqual(bot.calls, [])

    async def test_add_tool_call_updates_draft_when_enabled(self):
        bot = _BotRecorder()
        handler = StreamingMessageHandler(bot=bot, chat_id=42, user_id=7)
        handler.enable_tool_calls = True

        ok = await handler.add_tool_call("Read", {"file_path": "/tmp/a.txt"})

        self.assertTrue(ok)
        self.assertIn("**Read**", handler.tool_calls_text)
        self.assertEqual(len(bot.calls), 1)
        self.assertEqual(bot.calls[0][0], "send_message")
        self.assertIn("/tmp/a.txt", bot.calls[0][2])

    async def test_large_subchunk_block_is_one_send_no_edit_storm(self):
        """A single complete SDK block under the 4000-char limit must produce
        exactly one send and NO per-slice edit storm.

        Regression for the streaming latency bug: the SDK delivers complete text
        blocks (partial streaming off), and the old code sliced each block into
        min_chars pieces, firing one edit_message_text per slice (~len/min_chars
        sequential round-trips that also tripped Telegram's edit flood limit).
        """
        bot = _BotRecorder()
        handler = StreamingMessageHandler(bot=bot, chat_id=42, user_id=7)

        await handler.update_if_needed("x" * 1500)  # ~10 slices under old code

        sends = [c for c in bot.calls if c[0] == "send_message"]
        edits = [c for c in bot.calls if c[0] == "edit_message_text"]
        self.assertEqual(len(sends), 1)
        self.assertEqual(len(edits), 0)
        self.assertEqual(len(handler.drafts), 1)

    async def test_max_bubble_chars_controls_split(self):
        """A smaller per-bubble size splits a long reply into more, smaller drafts."""
        bot = _BotRecorder()
        handler = StreamingMessageHandler(bot=bot, chat_id=42, user_id=7)
        handler.max_bubble_chars = 1000

        await handler.update_if_needed("x" * 3500)

        # 3500 / 1000 -> 4 drafts (three full bubbles + a remainder).
        self.assertEqual(len(handler.drafts), 4)
        for draft in handler.drafts[:-1]:
            self.assertLessEqual(len(draft.text), 1000)

    async def test_first_chunk_creates_draft_immediately(self):
        """First paint must not wait for min_chars/min_interval thresholds."""
        bot = _BotRecorder()
        handler = StreamingMessageHandler(bot=bot, chat_id=42, user_id=7)
        handler.min_chars = 10_000
        handler.min_interval = 60.0

        await handler.update_if_needed("hi")

        self.assertEqual(len(handler.drafts), 1)
        self.assertEqual(handler.drafts[0].text, "hi")
        self.assertEqual([c[0] for c in bot.calls], ["send_message"])

    async def test_semantic_split_prefers_paragraph_boundary_in_back_half(self):
        bot = _BotRecorder()
        handler = StreamingMessageHandler(bot=bot, chat_id=42, user_id=7)
        text = "a" * 650 + "\n\n" + "b" * 700

        split = handler._find_split_boundary(text, max_length=1000)

        self.assertEqual(split, 652)

    async def test_semantic_split_does_not_cut_inside_code_fence_when_possible(self):
        bot = _BotRecorder()
        handler = StreamingMessageHandler(bot=bot, chat_id=42, user_id=7)
        prefix = "intro " * 60
        text = prefix + "\n\n```\n" + ("code line\n" * 90) + "```\n\nafter"

        split = handler._find_split_boundary(text, max_length=700)

        self.assertLessEqual(split, len(prefix) + len("\n\n"))

    async def test_default_bubble_size_falls_back_to_4000(self):
        bot = _BotRecorder()
        handler = StreamingMessageHandler(bot=bot, chat_id=42, user_id=7)
        # Test config stub has no telegram_max_bubble_chars -> safe default.
        self.assertEqual(handler.max_bubble_chars, 4000)

    async def test_large_block_api_calls_scale_with_drafts_not_length(self):
        """A multi-draft block splits by the 4000-char limit, and the number of
        Telegram API calls tracks the draft count — not the text length."""
        bot = _BotRecorder()
        handler = StreamingMessageHandler(bot=bot, chat_id=42, user_id=7)

        await handler.update_if_needed("x" * 9000)  # ~60 slices under old code

        self.assertEqual(len(handler.drafts), 3)  # 9000 / 4000 -> 3 drafts
        # Bounded by ~2 calls per draft (a send to open each draft + a finalize
        # edit when it overflows), far below the ~60 edit_message_text calls the
        # per-slice implementation would have made for 9000 chars.
        self.assertLessEqual(len(bot.calls), 2 * len(handler.drafts))
        self.assertLess(len(bot.calls), 9000 // handler.min_chars)


from telegram_bot.utils import tg_md  # noqa: E402

_HAS_TG_MD = tg_md.available()


class FinalizeMarkdownV2SplitTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(_HAS_TG_MD, "telegramify-markdown not installed")
    async def test_small_draft_single_markdownv2_edit(self):
        bot = _BotCapture()
        handler = StreamingMessageHandler(bot=bot, chat_id=42, user_id=7)
        draft = SimpleNamespace(message_id=992, text="간단 보고: a_b*c (괄호)")

        ok = await handler.finalize_draft(draft)

        self.assertTrue(ok)
        self.assertEqual(len(bot.edits), 1)
        self.assertEqual(bot.edits[0][0], 992)
        self.assertEqual(bot.edits[0][2], "MarkdownV2")
        self.assertEqual(bot.sends, [])  # no overflow follow-ups

    @unittest.skipUnless(_HAS_TG_MD, "telegramify-markdown not installed")
    async def test_overflow_draft_splits_into_followups(self):
        # Symbol-dense body whose MarkdownV2-escaped form exceeds TELEGRAM_LIMIT.
        # Previously this dropped the WHOLE draft to plain text (formatting lost);
        # now the draft is upgraded to chunk 1 and the rest go as follow-ups.
        draft_text = (
            "- 항목 a_b*c (괄호) {중괄호} [링크](http://x) 1+1=2 경로/a.b-c #태그\n" * 90
        )
        md2 = tg_md.to_markdownv2(draft_text)
        self.assertGreater(tg_md.utf16_len(md2), tg_md.TELEGRAM_LIMIT)  # precondition

        bot = _BotCapture()
        handler = StreamingMessageHandler(bot=bot, chat_id=42, user_id=7)
        draft = SimpleNamespace(message_id=992, text=draft_text)

        ok = await handler.finalize_draft(draft)

        self.assertTrue(ok)
        # The original draft message is upgraded in place to MarkdownV2 chunk 1...
        self.assertEqual(len(bot.edits), 1)
        self.assertEqual(bot.edits[0][0], 992)
        self.assertEqual(bot.edits[0][2], "MarkdownV2")
        self.assertLessEqual(tg_md.utf16_len(bot.edits[0][1]), tg_md.TELEGRAM_LIMIT)
        # ...and the overflow goes out as MarkdownV2 follow-up messages, each
        # within the per-message limit (no plain-text fallback, formatting kept).
        self.assertGreaterEqual(len(bot.sends), 1)
        for text, parse_mode in bot.sends:
            self.assertEqual(parse_mode, "MarkdownV2")
            self.assertLessEqual(tg_md.utf16_len(text), tg_md.TELEGRAM_LIMIT)

    @unittest.skipUnless(_HAS_TG_MD, "telegramify-markdown not installed")
    async def test_streaming_overflow_drafts_get_part_headers(self):
        """Part headers must fire for streaming overflow drafts, not only final chunks.

        Regression coverage for #57: streaming overflow used to finalize each
        ~4K draft as a single chunk, so CCC_TELEGRAM_PART_HEADERS=true never
        produced k/N markers for the default streaming path.
        """
        bot = _BotCapture()
        handler = StreamingMessageHandler(bot=bot, chat_id=42, user_id=7)
        handler.min_chars = 150
        long_body = ("word " * 1800).rstrip()

        config_module.config.enable_part_headers = True
        try:
            await handler.update_if_needed(long_body)
            ok = await handler.finalize_all()
        finally:
            config_module.config.enable_part_headers = False

        self.assertTrue(ok)
        self.assertGreater(len(handler.drafts), 1, "precondition: streaming overflow happened")
        marked_edits = [
            text for _message_id, text, parse_mode in bot.edits
            if parse_mode == "MarkdownV2" and text.startswith("*1/")
        ]
        self.assertTrue(marked_edits, "streaming multi-draft response missing k/N header")
        total = len(handler.drafts)
        final_marked_texts = [
            text for _message_id, text, parse_mode in bot.edits
            if parse_mode == "MarkdownV2" and any(
                text.startswith(f"*{index}/{total}*") for index in range(1, total + 1)
            )
        ]
        self.assertGreaterEqual(len(final_marked_texts), total)


class SplitBoundaryGuardTests(unittest.TestCase):
    def setUp(self):
        self.handler = StreamingMessageHandler(
            bot=_BotRecorder(), chat_id=42, user_id=7
        )

    def test_avoid_split_inside_pipe_table(self):
        head = "h" * 60 + "\n"  # 61 chars, no blank line in window
        table = "| a | b |\n" * 6  # contiguous pipe table
        text = head + table + "end\n"
        # A naive line-boundary cut inside the table (after 3 rows)...
        naive_cut = len(head) + 30
        cut = self.handler._avoid_block_split(text, naive_cut, max_length=100)
        # ...is pulled back to the table block start, so no row is straddled.
        self.assertEqual(cut, len(head))
        self.assertTrue(text[cut:].startswith("| a | b |"))

    def test_avoid_split_inside_code_fence(self):
        head = "h" * 60 + "\n"
        body = "```\n" + "code\n" * 8
        text = head + body
        naive_cut = len(head) + 4 + 25  # inside the unclosed code block
        cut = self.handler._avoid_block_split(text, naive_cut, max_length=100)
        self.assertEqual(cut, len(head))
        self.assertTrue(text[cut:].startswith("```"))

    def test_no_block_returns_cut_unchanged(self):
        text = "para one line\n" * 20
        cut = 100
        self.assertEqual(
            self.handler._avoid_block_split(text, cut, max_length=100), cut
        )

    def test_tiny_backup_floored(self):
        # Table starts below the floor (max_length//2); guard declines to back up
        # that far rather than emit a pathologically small chunk.
        table = "| a | b |\n" * 10
        text = table + "end\n"
        naive_cut = 30  # inside the table
        cut = self.handler._avoid_block_split(text, naive_cut, max_length=100)
        self.assertEqual(cut, naive_cut)


class EntityFinalizeTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(_ENTITY_OK, "entity API unavailable")
    async def test_entity_path_sends_entities_without_parse_mode(self):
        class _EntityBot:
            def __init__(self):
                self.edits = []
                self.sends = []

            async def edit_message_text(
                self, *, chat_id, message_id, text, parse_mode=None, entities=None
            ):
                self.edits.append((text, parse_mode, entities))
                return SimpleNamespace(message_id=message_id)

            async def send_message(
                self, *, chat_id, text, parse_mode=None, entities=None
            ):
                self.sends.append((text, parse_mode, entities))
                return SimpleNamespace(message_id=900)

        bot = _EntityBot()
        handler = StreamingMessageHandler(bot=bot, chat_id=42, user_id=7)
        draft = SimpleNamespace(message_id=10, text="**bold** text")

        config_module.config.enable_entity_renderer = True
        try:
            ok = await handler.finalize_draft(draft)
        finally:
            config_module.config.enable_entity_renderer = False

        self.assertTrue(ok)
        self.assertEqual(len(bot.edits), 1)
        text, parse_mode, entities = bot.edits[0]
        self.assertIsNone(parse_mode)  # entities and parse_mode are mutually exclusive
        self.assertTrue(entities)
        self.assertNotIn("**", text)  # markdown markup moved into entities

    @unittest.skipUnless(_ENTITY_OK, "entity API unavailable")
    async def test_entity_path_applies_part_headers_on_multi_chunk(self):
        """Multi-chunk responses on the entity path get bold k/N markers.

        Regression coverage for the entity-vs-MarkdownV2 part-headers gap: until
        this lane was added, ``apply_part_headers`` only ran on the MarkdownV2
        fallback, so the default-on entity renderer emitted multi-bubble
        responses with no part marker at all.
        """
        from telegram import MessageEntity

        class _EntityBot:
            def __init__(self):
                self.edits = []
                self.sends = []

            async def edit_message_text(
                self, *, chat_id, message_id, text, parse_mode=None, entities=None
            ):
                self.edits.append((text, parse_mode, entities))
                return SimpleNamespace(message_id=message_id)

            async def send_message(
                self, *, chat_id, text, parse_mode=None, entities=None
            ):
                self.sends.append((text, parse_mode, entities))
                return SimpleNamespace(message_id=900)

        bot = _EntityBot()
        handler = StreamingMessageHandler(bot=bot, chat_id=42, user_id=7)
        # ~12K chars of ASCII → guaranteed >1 chunk under TELEGRAM_LIMIT=4096
        long_body = ("word " * 2400).rstrip()
        draft = SimpleNamespace(message_id=10, text=long_body)

        config_module.config.enable_entity_renderer = True
        config_module.config.enable_part_headers = True
        try:
            ok = await handler.finalize_draft(draft)
        finally:
            config_module.config.enable_entity_renderer = False
            config_module.config.enable_part_headers = False

        self.assertTrue(ok)
        # First chunk lands via edit; remaining chunks via send_message.
        total_chunks = 1 + len(bot.sends)
        self.assertGreater(total_chunks, 1, "draft should split into >1 chunks")

        # Every chunk must start with 'k/N\n' and carry a bold marker entity.
        all_chunks = [
            (bot.edits[0][0], bot.edits[0][2]),
            *[(text, entities) for text, _pm, entities in bot.sends],
        ]
        for index, (text, entities) in enumerate(all_chunks, 1):
            expected_prefix = f"{index}/{total_chunks}\n"
            self.assertTrue(
                text.startswith(expected_prefix),
                f"chunk {index} text missing 'k/N' prefix: {text[:20]!r}",
            )
            self.assertTrue(entities, f"chunk {index} has no entities")
            marker = entities[0]
            self.assertEqual(marker.type, MessageEntity.BOLD)
            self.assertEqual(marker.offset, 0)
            self.assertEqual(marker.length, len(f"{index}/{total_chunks}"))


if __name__ == "__main__":
    unittest.main()
