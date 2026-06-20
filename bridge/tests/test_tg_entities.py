import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from telegram_bot.utils import tg_entities

_HAS = tg_entities.available()
needs_lib = unittest.skipUnless(_HAS, "telegramify-markdown entity API unavailable")


class EntityRenderTests(unittest.TestCase):
    def test_empty_returns_none(self):
        self.assertIsNone(tg_entities.to_entity_chunks(""))

    @needs_lib
    def test_basic_conversion_strips_markup_to_entities(self):
        chunks = tg_entities.to_entity_chunks("**bold** and `code`")
        self.assertIsNotNone(chunks)
        text, entities = chunks[0]
        self.assertIn("bold", text)
        self.assertNotIn("**", text)  # markup is moved into entities, not text
        types = {e.type for e in entities}
        self.assertIn("bold", types)
        self.assertIn("code", types)

    @needs_lib
    def test_entities_are_ptb_message_entities(self):
        from telegram import MessageEntity

        _, entities = tg_entities.to_entity_chunks("**x**")[0]
        self.assertTrue(entities)
        self.assertTrue(all(isinstance(e, MessageEntity) for e in entities))

    @needs_lib
    def test_chunking_respects_utf16_limit(self):
        import telegramify_markdown as tm

        chunks = tg_entities.to_entity_chunks("word " * 2000, limit=1000)
        self.assertIsNotNone(chunks)
        self.assertGreater(len(chunks), 1)
        for text, _ in chunks:
            self.assertLessEqual(tm.utf16_len(text), 1000)

    @needs_lib
    def test_link_entity_offset_is_utf16_safe(self):
        import telegramify_markdown as tm

        # CJK + emoji before the link force UTF-16 (not codepoint) offsets.
        text, entities = tg_entities.to_entity_chunks(
            "가나다 😀 [x](https://e.com)"
        )[0]
        links = [e for e in entities if e.type == "text_link"]
        self.assertTrue(links)
        e = links[0]
        self.assertEqual(e.url, "https://e.com")
        self.assertLessEqual(e.offset + e.length, tm.utf16_len(text))


if __name__ == "__main__":
    unittest.main()
