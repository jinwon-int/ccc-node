# pyright: reportMissingImports=false
"""
Tests for revert command functionality.
"""

import tempfile
import unittest
from pathlib import Path

from telegram_bot.core.conversation_paths import resolve_conversation_file


class TestConversationPathResolution(unittest.TestCase):
    """Test conversation JSONL path containment checks."""

    def test_resolves_session_file_under_conversations_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "conversations"
            root.mkdir()

            self.assertEqual(
                resolve_conversation_file(root, "session-123"),
                root.resolve() / "session-123.jsonl",
            )

    def test_rejects_path_traversal_session_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "conversations"
            root.mkdir()

            self.assertIsNone(resolve_conversation_file(root, "../outside/session"))

    def test_rejects_absolute_session_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "conversations"
            root.mkdir()

            self.assertIsNone(resolve_conversation_file(root, "/tmp/not-a-session"))

    def test_rejects_symlink_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "conversations"
            root.mkdir()
            outside = base / "outside.jsonl"
            outside.write_text("outside\n", encoding="utf-8")
            (root / "link.jsonl").symlink_to(outside)

            self.assertIsNone(resolve_conversation_file(root, "link"))


class TestRevertCallbackParsing(unittest.TestCase):
    """Test callback data parsing for revert operations."""

    def test_parse_select_callback(self):
        """Test parsing message selection callback."""
        data = "revert:select:42"
        parts = data.split(":")

        self.assertEqual(parts[0], "revert")
        self.assertEqual(parts[1], "select")
        self.assertEqual(int(parts[2]), 42)

    def test_parse_page_callback(self):
        """Test parsing pagination callback."""
        data = "revert:page:3"
        parts = data.split(":")

        self.assertEqual(parts[0], "revert")
        self.assertEqual(parts[1], "page")
        self.assertEqual(int(parts[2]), 3)

    def test_parse_mode_callback(self):
        """Test parsing mode selection callback."""
        data = "revert:mode:42:full"
        parts = data.split(":")

        self.assertEqual(parts[0], "revert")
        self.assertEqual(parts[1], "mode")
        self.assertEqual(int(parts[2]), 42)
        self.assertEqual(parts[3], "full")

    def test_parse_mode_callback_all_modes(self):
        """Test parsing all revert modes."""
        modes = ["full", "conv", "code", "summary", "cancel"]

        for mode in modes:
            data = f"revert:mode:10:{mode}"
            parts = data.split(":")

            self.assertEqual(parts[0], "revert")
            self.assertEqual(parts[1], "mode")
            self.assertEqual(int(parts[2]), 10)
            self.assertEqual(parts[3], mode)


if __name__ == "__main__":
    unittest.main()
