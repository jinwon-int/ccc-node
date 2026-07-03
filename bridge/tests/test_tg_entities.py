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


class EntityPartHeaderTests(unittest.TestCase):
    """Entity-path counterpart to tg_readable.apply_part_headers tests."""

    def test_single_chunk_unchanged(self):
        src = [("only", [])]
        out = tg_entities.apply_part_headers(src)
        self.assertEqual(out, [("only", [])])

    def test_empty_unchanged(self):
        self.assertEqual(tg_entities.apply_part_headers([]), [])

    def test_returns_new_list_without_mutating_input(self):
        src = [("a", []), ("b", [])]
        out = tg_entities.apply_part_headers(src)
        self.assertIsNot(out, src)
        # input chunks themselves untouched
        self.assertEqual(src[0], ("a", []))
        self.assertEqual(src[1], ("b", []))

    @needs_lib
    def test_multi_chunk_prepends_bold_marker(self):
        from telegram import MessageEntity

        src = [("alpha", []), ("beta", []), ("gamma", [])]
        out = tg_entities.apply_part_headers(src)
        self.assertEqual(len(out), 3)
        for index, (text, entities) in enumerate(out, 1):
            self.assertTrue(text.startswith(f"{index}/3\n"))
            # first entity is the bold marker over the 'k/N' digits only
            self.assertEqual(entities[0].type, MessageEntity.BOLD)
            self.assertEqual(entities[0].offset, 0)
            self.assertEqual(entities[0].length, len(f"{index}/3"))

    @needs_lib
    def test_multi_chunk_shifts_existing_entity_offsets(self):
        from telegram import MessageEntity

        # Pre-existing bold over "alpha" at offset 0, length 5
        existing = MessageEntity(type="bold", offset=0, length=5)
        src = [("alpha body", [existing]), ("beta body", [])]
        out = tg_entities.apply_part_headers(src)

        first_text, first_entities = out[0]
        prefix = "1/2\n"  # marker + newline
        self.assertEqual(first_text, prefix + "alpha body")
        # Two entities: marker bold (offset 0) + shifted original (offset = len(prefix))
        self.assertEqual(len(first_entities), 2)
        marker, shifted = first_entities
        self.assertEqual(marker.offset, 0)
        self.assertEqual(marker.length, 3)  # "1/2"
        self.assertEqual(shifted.type, MessageEntity.BOLD)
        self.assertEqual(shifted.offset, len(prefix))
        self.assertEqual(shifted.length, 5)

    @needs_lib
    def test_offset_shift_is_utf16_aware_for_ascii_marker(self):
        import telegramify_markdown as tm

        # ASCII-only marker — UTF-16 length must equal the string length so the
        # shifted offset is byte-for-byte addressable on Telegram's side.
        src = [("x", []), ("y", [])]
        out = tg_entities.apply_part_headers(src)
        prefix_text = "1/2\n"
        self.assertEqual(tm.utf16_len(prefix_text), len(prefix_text))
        # marker entity covers exactly the digit/slash portion (no newline)
        self.assertEqual(out[0][1][0].length, len("1/2"))
        # full chunk text starts with the prefix
        self.assertTrue(out[0][0].startswith(prefix_text))


class RenderedSpacingTests(unittest.TestCase):
    @needs_lib
    def test_rendered_gaps_are_uniform_across_boundary_types(self):
        # Entity-path twin of the tg_md uniformity test: the readable
        # renderer's per-boundary filler must survive entity conversion so the
        # visible spacing is consistent (spacing between blocks, spacing-1
        # between list items, one blank line under a heading).
        from telegram_bot.utils.tg_readable import to_readable

        doc = (
            "## Title\n\nintro para\n\n- item one\n- item two\n\n"
            "closing para\n\nsecond para"
        )
        text, _ = tg_entities.to_entity_chunks(
            to_readable(doc, loose=True, spacing=2), 4000
        )[0]

        def gap(a, b):
            lines = text.split("\n")
            ai = next(i for i, l in enumerate(lines) if a in l)
            bi = next(i for i, l in enumerate(lines) if b in l and i > ai)
            return sum(1 for l in lines[ai + 1 : bi] if l.strip() == "")

        self.assertEqual(gap("Title", "intro"), 1)
        self.assertEqual(gap("intro", "item one"), 2)
        self.assertEqual(gap("item one", "item two"), 1)
        self.assertEqual(gap("item two", "closing"), 2)
        self.assertEqual(gap("closing", "second"), 2)


if __name__ == "__main__":
    unittest.main()
