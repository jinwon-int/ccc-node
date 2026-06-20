import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from telegram_bot.utils.tg_readable import to_readable


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


if __name__ == "__main__":
    unittest.main()
