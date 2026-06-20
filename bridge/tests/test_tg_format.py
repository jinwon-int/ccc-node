import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from telegram_bot.utils.tg_format import wrap_markdown_tables


class WrapMarkdownTablesTest(unittest.TestCase):
    def test_no_table_passthrough(self):
        text = "Just a sentence.\nAnother line with a | pipe but no table."
        self.assertEqual(wrap_markdown_tables(text), text)

    def test_empty_and_none_safe(self):
        self.assertEqual(wrap_markdown_tables(""), "")
        self.assertEqual(wrap_markdown_tables(None), None)

    def test_horizontal_rule_not_treated_as_table(self):
        # A lone --- after a normal line must not be consumed as a delimiter.
        text = "Heading\n---\nbody"
        self.assertEqual(wrap_markdown_tables(text), text)

    def test_simple_table_to_bullets(self):
        text = (
            "| Name | Role |\n"
            "|------|------|\n"
            "| alice | admin |\n"
            "| bob | user |"
        )
        out = wrap_markdown_tables(text)
        self.assertNotIn("|------|", out)
        self.assertIn("**alice**", out)
        self.assertIn("• Role: admin", out)
        self.assertIn("**bob**", out)
        self.assertIn("• Role: user", out)
        # heading value must not be duplicated as its own bullet
        self.assertNotIn("• Name: alice", out)

    def test_row_label_column_detected(self):
        # Data rows have one more cell than the header -> first cell is heading.
        text = (
            "| 영역 | 상태 |\n"
            "|---|---|\n"
            "| Team1 | seoseo | ok |\n"
        )
        out = wrap_markdown_tables(text)
        self.assertIn("**Team1**", out)
        self.assertIn("• 영역: seoseo", out)
        self.assertIn("• 상태: ok", out)

    def test_table_inside_code_fence_preserved(self):
        text = (
            "```\n"
            "| a | b |\n"
            "|---|---|\n"
            "| 1 | 2 |\n"
            "```"
        )
        # Inside a fence the table must be left exactly as-is.
        self.assertEqual(wrap_markdown_tables(text), text)

    def test_surrounding_text_preserved(self):
        text = (
            "Before.\n"
            "| k | v |\n"
            "|---|---|\n"
            "| x | y |\n"
            "After."
        )
        out = wrap_markdown_tables(text)
        self.assertTrue(out.startswith("Before."))
        self.assertTrue(out.rstrip().endswith("After."))


if __name__ == "__main__":
    unittest.main()
