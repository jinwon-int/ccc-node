"""Direct unit tests for conversation-JSONL truncation (core/revert.py).

This is the data-loss-sensitive core of the /revert flow. The existing
test_revert.py covers session-path resolution and callback parsing but never the
actual file truncation, which previously lived inline on the bot. These tests
pin the off-by-one semantics (revert to the state BEFORE the chosen message),
the atomic rewrite, and the failure modes against real temp files.
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from telegram_bot.core import revert


class LinesToKeepTest(unittest.TestCase):
    def test_keeps_strictly_before_index(self):
        lines = ["a\n", "b\n", "c\n", "d\n"]
        self.assertEqual(revert.lines_to_keep(lines, 2), ["a\n", "b\n"])

    def test_index_zero_keeps_nothing(self):
        self.assertEqual(revert.lines_to_keep(["a\n", "b\n"], 0), [])

    def test_negative_keeps_nothing(self):
        self.assertEqual(revert.lines_to_keep(["a\n"], -1), [])

    def test_index_past_end_keeps_all(self):
        lines = ["a\n", "b\n"]
        self.assertEqual(revert.lines_to_keep(lines, 99), ["a\n", "b\n"])


class TruncateJsonlTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _write(self, name: str, n: int) -> Path:
        p = self.dir / name
        p.write_text("".join(f"line{i}\n" for i in range(n)), encoding="utf-8")
        return p

    def test_truncates_to_before_message(self):
        p = self._write("c.jsonl", 5)  # line0..line4
        self.assertTrue(revert.truncate_jsonl_to(p, 3))
        self.assertEqual(p.read_text(), "line0\nline1\nline2\n")

    def test_index_zero_empties_file(self):
        p = self._write("c.jsonl", 4)
        self.assertTrue(revert.truncate_jsonl_to(p, 0))
        self.assertEqual(p.read_text(), "")

    def test_index_past_end_keeps_all(self):
        p = self._write("c.jsonl", 3)
        self.assertTrue(revert.truncate_jsonl_to(p, 100))
        self.assertEqual(p.read_text(), "line0\nline1\nline2\n")

    def test_missing_file_returns_false(self):
        self.assertFalse(revert.truncate_jsonl_to(self.dir / "nope.jsonl", 1))

    def test_no_temp_file_left_behind(self):
        p = self._write("c.jsonl", 4)
        self.assertTrue(revert.truncate_jsonl_to(p, 2))
        leftovers = [q.name for q in self.dir.iterdir() if q.name != "c.jsonl"]
        self.assertEqual(leftovers, [])

    def test_original_untouched_on_directory_target(self):
        # Pointing at a directory makes open() raise -> returns False, and the
        # target (a dir) is unchanged. Exercises the error path safely.
        d = self.dir / "subdir"
        d.mkdir()
        self.assertFalse(revert.truncate_jsonl_to(d, 1))
        self.assertTrue(d.is_dir())


if __name__ == "__main__":
    unittest.main()
