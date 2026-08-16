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

    def test_repeated_value_in_other_column_is_kept(self):
        # Regression (#869 sweep / #1076): the heading bullet used to be
        # suppressed by VALUE, so any other column repeating the heading text
        # was silently dropped instead of rendered.
        text = (
            "| Owner | Reviewer |\n"
            "|---|---|\n"
            "| alice | alice |"
        )
        out = wrap_markdown_tables(text)
        self.assertIn("**alice**", out)
        # The heading's own column stays suppressed ...
        self.assertNotIn("• Owner: alice", out)
        # ... but the second column's datum must survive.
        self.assertIn("• Reviewer: alice", out)

    def test_leading_blank_cell_suppresses_only_the_heading_column(self):
        # The heading is the first NON-EMPTY cell, so it is not always column 0.
        # Suppression must follow that column, not the literal index 0.
        text = (
            "| A | B | C |\n"
            "|---|---|---|\n"
            "|  | bob | bob |"
        )
        out = wrap_markdown_tables(text)
        self.assertIn("**bob**", out)
        self.assertIn("• A: ", out)          # empty leading cell still rendered
        self.assertNotIn("• B: bob", out)    # heading's own column suppressed
        self.assertIn("• C: bob", out)       # trailing duplicate survives

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
