"""Direct unit tests for the extracted pure presentation helpers (core/ui.py).

Before the extraction these helpers lived on the TelegramBot god object and were
only exercised indirectly. Testing them directly pins down the split/format/
keyboard behavior so future refactors can't regress it silently.
"""

import unittest
from datetime import datetime, timedelta, timezone

from telegram_bot.core import ui


class SplitTextTest(unittest.TestCase):
    def test_short_text_is_single_chunk(self):
        self.assertEqual(ui.split_text("hello", limit=4000), ["hello"])

    def test_each_chunk_within_limit(self):
        text = "\n".join(f"line {i}" for i in range(1000))
        chunks = ui.split_text(text, limit=100)
        self.assertTrue(len(chunks) > 1)
        for c in chunks:
            self.assertLessEqual(len(c), 100)

    def test_prefers_paragraph_boundary(self):
        text = "a" * 50 + "\n\n" + "b" * 50
        chunks = ui.split_text(text, limit=60)
        self.assertEqual(chunks[0], "a" * 50)
        self.assertEqual(chunks[1], "b" * 50)

    def test_hard_cut_when_no_boundary(self):
        text = "x" * 250
        chunks = ui.split_text(text, limit=100)
        self.assertEqual(chunks, ["x" * 100, "x" * 100, "x" * 50])

    def test_no_content_lost(self):
        text = "para one\n\npara two\n\npara three " + "y" * 200
        chunks = ui.split_text(text, limit=40)
        # Joining stripped chunks should preserve every non-whitespace char.
        joined = "".join(chunks).replace(" ", "").replace("\n", "")
        original = text.replace(" ", "").replace("\n", "")
        self.assertEqual(joined, original)


class ExtractOptionsTest(unittest.TestCase):
    def test_consecutive_numbered_options(self):
        self.assertEqual(
            ui.extract_options("1. apple\n2. banana\n3. cherry"),
            ["apple", "banana", "cherry"],
        )

    def test_cjk_delimiters(self):
        self.assertEqual(ui.extract_options("1、foo\n2）bar"), ["foo", "bar"])

    def test_single_option_rejected(self):
        self.assertEqual(ui.extract_options("1. only one"), [])

    def test_non_consecutive_rejected(self):
        self.assertEqual(ui.extract_options("1. a\n3. c"), [])

    def test_plain_text_yields_nothing(self):
        self.assertEqual(ui.extract_options("just a sentence"), [])


class FormatRelativeTimeTest(unittest.TestCase):
    @staticmethod
    def _iso(delta: timedelta) -> str:
        return (datetime.now(timezone.utc) - delta).isoformat()

    def test_empty(self):
        self.assertEqual(ui.format_relative_time(""), "")

    def test_just_now(self):
        self.assertEqual(ui.format_relative_time(self._iso(timedelta(seconds=10))), "Just now")

    def test_minutes(self):
        self.assertEqual(ui.format_relative_time(self._iso(timedelta(minutes=5))), "5m ago")

    def test_hours(self):
        self.assertEqual(ui.format_relative_time(self._iso(timedelta(hours=3))), "3h ago")

    def test_yesterday(self):
        self.assertEqual(ui.format_relative_time(self._iso(timedelta(days=1, hours=1))), "Yesterday")

    def test_days(self):
        self.assertEqual(ui.format_relative_time(self._iso(timedelta(days=2, hours=1))), "2d ago")

    def test_malformed_falls_back_to_prefix(self):
        self.assertEqual(ui.format_relative_time("2026-01-02 garbage"), "2026-01-02")


class KeyboardTest(unittest.TestCase):
    def test_option_keyboard_empty_is_none(self):
        self.assertIsNone(ui.build_option_keyboard([]))

    def test_option_keyboard_callback_data(self):
        kb = ui.build_option_keyboard(["yes", "no"])
        rows = kb.inline_keyboard
        self.assertEqual(rows[0][0].callback_data, "opt:1. yes")
        self.assertEqual(rows[1][0].callback_data, "opt:2. no")

    def test_option_keyboard_truncates_long_callback(self):
        long_opt = "z" * 200
        kb = ui.build_option_keyboard([long_opt])
        cb = kb.inline_keyboard[0][0].callback_data
        self.assertEqual(cb, "opt:1")
        self.assertLessEqual(len(cb.encode("utf-8")), 64)

    def test_history_keyboard_pagination_first_page(self):
        messages = [{"index": i, "timestamp": "", "content": f"m{i}"} for i in range(25)]
        kb = ui.build_history_keyboard(messages, page=0, page_size=10)
        labels = [b.text for row in kb.inline_keyboard for b in row]
        # First page: no "Previous", has "Next".
        self.assertFalse(any("Previous" in label for label in labels))
        self.assertTrue(any("Next" in label for label in labels))

    def test_history_keyboard_pagination_middle_page(self):
        messages = [{"index": i, "timestamp": "", "content": f"m{i}"} for i in range(25)]
        kb = ui.build_history_keyboard(messages, page=1, page_size=10)
        labels = [b.text for row in kb.inline_keyboard for b in row]
        self.assertTrue(any("Previous" in label for label in labels))
        self.assertTrue(any("Next" in label for label in labels))

    def test_history_keyboard_select_callback(self):
        messages = [{"index": 7, "timestamp": "", "content": "hello"}]
        kb = ui.build_history_keyboard(messages, page=0, page_size=10)
        self.assertEqual(kb.inline_keyboard[0][0].callback_data, "revert:select:7")

    def test_revert_mode_keyboard_modes(self):
        kb = ui.build_revert_mode_keyboard(3)
        cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
        self.assertEqual(
            cbs,
            [
                "revert:mode:3:full",
                "revert:mode:3:conv",
                "revert:mode:3:code",
                "revert:mode:3:summary",
                "revert:mode:3:cancel",
            ],
        )


if __name__ == "__main__":
    unittest.main()
