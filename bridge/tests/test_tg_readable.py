import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from telegram_bot.utils.tg_readable import (
    to_readable,
    render_for_delivery,
    apply_part_headers,
    part_marker,
)


class ToReadableTests(unittest.TestCase):
    def test_empty_passthrough(self):
        self.assertEqual(to_readable(""), "")

    def test_strips_trailing_whitespace(self):
        self.assertEqual(to_readable("a   \nb\t"), "a\nb")

    def test_collapses_blank_runs(self):
        self.assertEqual(to_readable("a\n\n\n\nb"), "a\n\nb")

    def test_trims_leading_and_trailing_blanks(self):
        self.assertEqual(to_readable("\n\nhello\n\n"), "hello")

    def test_blank_line_before_heading(self):
        self.assertEqual(
            to_readable("intro text\n## Section\nbody"),
            "intro text\n\n## Section\nbody",
        )

    def test_blank_line_before_bold_label(self):
        self.assertEqual(
            to_readable("intro\n**확인됨**\n- item"),
            "intro\n\n**확인됨**\n- item",
        )

    def test_no_extra_blank_when_already_separated(self):
        self.assertEqual(
            to_readable("intro\n\n## Section\nbody"),
            "intro\n\n## Section\nbody",
        )

    def test_fenced_code_block_is_untouched(self):
        src = "## Code\n```\n  indented   \n\n\n  kept\n```\ntail"
        out = to_readable(src)
        # Trailing spaces and blank runs inside the fence must be preserved.
        self.assertIn("  indented   \n\n\n  kept", out)

    def test_idempotent(self):
        src = "intro text  \n## A\n\n\n\n- x\n- y\n**다음**\nz   "
        once = to_readable(src)
        self.assertEqual(once, to_readable(once))

    def test_fail_open_returns_input_unchanged_on_error(self):
        sentinel = object()  # no .split -> exercises the fail-open path
        # The failure is logged (captured here, not printed) and the input is
        # returned unchanged so formatting never costs a message.
        with self.assertLogs("telegram_bot.utils.tg_readable", level="WARNING"):
            self.assertIs(to_readable(sentinel), sentinel)

    def test_snapshot_operational_report(self):
        before = (
            "요약입니다.\n"
            "## 확정\n"
            "- nosuk healthy\n"
            "- soonwook healthy\n"
            "## 변경\n"
            "PR #33 merged   \n"
            "\n\n\n"
            "## 다음\n"
            "재시작 확인"
        )
        after = (
            "요약입니다.\n"
            "\n"
            "## 확정\n"
            "- nosuk healthy\n"
            "- soonwook healthy\n"
            "\n"
            "## 변경\n"
            "PR #33 merged\n"
            "\n"
            "## 다음\n"
            "재시작 확인"
        )
        self.assertEqual(to_readable(before), after)


class LooseSpacingTests(unittest.TestCase):
    def test_loose_off_by_default(self):
        # Default call leaves tight list items unchanged.
        self.assertEqual(to_readable("- a\n- b\n- c"), "- a\n- b\n- c")

    def test_loose_separates_list_items(self):
        self.assertEqual(
            to_readable("- a\n- b\n- c", loose=True),
            "- a\n\n- b\n\n- c",
        )

    def test_loose_separates_numbered_list_items(self):
        self.assertEqual(
            to_readable("1. a\n2. b", loose=True),
            "1. a\n\n2. b",
        )

    def test_loose_leaves_prose_lines_attached(self):
        # Only list items get spaced; adjacent prose lines stay together.
        self.assertEqual(
            to_readable("first line\nsecond line", loose=True),
            "first line\nsecond line",
        )

    def test_loose_does_not_split_list_item_continuation(self):
        # An indented continuation line is not a list item, so it stays attached.
        self.assertEqual(
            to_readable("- item one\n  more detail\n- item two", loose=True),
            "- item one\n  more detail\n- item two",
        )

    def test_loose_keeps_table_rows_together(self):
        src = "| h1 | h2 |\n| -- | -- |\n| a | b |"
        # Table rows are not list items, so they are never split.
        self.assertEqual(to_readable(src, loose=True), src)

    def test_loose_does_not_double_existing_blanks(self):
        self.assertEqual(
            to_readable("a\n\nb", loose=True),
            "a\n\nb",
        )

    def test_loose_leaves_fenced_code_untouched(self):
        src = "intro\n```\nline1\nline2\n```\ntail"
        out = to_readable(src, loose=True)
        # Code lines stay adjacent; only content outside the fence gets air.
        self.assertIn("```\nline1\nline2\n```", out)

    def test_loose_is_idempotent(self):
        src = "intro\n- a\n- b\n## Section\nbody line one\nbody line two"
        once = to_readable(src, loose=True)
        self.assertEqual(once, to_readable(once, loose=True))


class PartHeaderTests(unittest.TestCase):
    def test_part_marker_is_markdownv2_safe(self):
        # '*' is the only markup; digits and '/' need no MarkdownV2 escaping.
        self.assertEqual(part_marker(2, 3), "*2/3*")

    def test_single_chunk_unchanged(self):
        self.assertEqual(apply_part_headers(["only"]), ["only"])

    def test_empty_unchanged(self):
        self.assertEqual(apply_part_headers([]), [])

    def test_multi_chunk_gets_markers(self):
        self.assertEqual(
            apply_part_headers(["a", "b", "c"]),
            ["*1/3*\na", "*2/3*\nb", "*3/3*\nc"],
        )

    def test_returns_new_list_without_mutating_input(self):
        src = ["x", "y"]
        out = apply_part_headers(src)
        self.assertIsNot(out, src)
        self.assertEqual(src, ["x", "y"])


class RenderForDeliveryTests(unittest.TestCase):
    """Shared helper used by both the streaming and non-streaming send paths."""

    SRC = "Items:\n- a\n- b\n- c\n"

    def test_disabled_returns_input_unchanged(self):
        # When the readable renderer is off, the text must pass through verbatim
        # (no whitespace normalization, no loose spacing) regardless of loose.
        self.assertEqual(
            render_for_delivery(self.SRC, enabled=False, loose=True), self.SRC
        )
        self.assertEqual(
            render_for_delivery(self.SRC, enabled=False, loose=False), self.SRC
        )

    def test_enabled_loose_inserts_blank_lines_between_list_items(self):
        out = render_for_delivery(self.SRC, enabled=True, loose=True)
        self.assertEqual(out, "Items:\n- a\n\n- b\n\n- c")
        # Equivalent to calling to_readable directly with loose=True.
        self.assertEqual(out, to_readable(self.SRC, loose=True))

    def test_enabled_compact_normalizes_without_loose_spacing(self):
        out = render_for_delivery(self.SRC, enabled=True, loose=False)
        # No blank lines inserted between adjacent list items in compact mode.
        self.assertNotIn("\n\n", out)
        self.assertEqual(out, to_readable(self.SRC, loose=False))


if __name__ == "__main__":
    unittest.main()
