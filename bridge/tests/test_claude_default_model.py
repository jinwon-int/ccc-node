"""Unit tests for ``claude_default_model`` — the CLI's own resolution order.

``ANTHROPIC_MODEL`` (the bridge unit's env) wins over ``settings.json``
``model``; a blank env value counts as unset; an unreadable or model-less
settings file yields the caller's default.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from telegram_bot.core.claude_default_model import (
    claude_default_model,
    env_claude_model,
    settings_claude_model,
)


class ClaudeDefaultModelTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = Path(self.tmp.name) / "settings.json"
        self.settings.write_text(json.dumps({"model": "claude-opus-5-5"}))

    def test_env_wins_over_settings(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_MODEL": "claude-fable-5-1"}):
            self.assertEqual(claude_default_model(self.settings), "claude-fable-5-1")
            self.assertEqual(env_claude_model(), "claude-fable-5-1")

    def test_blank_env_falls_back_to_settings(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_MODEL": "   "}):
            self.assertIsNone(env_claude_model())
            self.assertEqual(claude_default_model(self.settings), "claude-opus-5-5")

    def test_unset_env_falls_back_to_settings(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ANTHROPIC_MODEL", None)
            self.assertEqual(settings_claude_model(self.settings), "claude-opus-5-5")
            self.assertEqual(claude_default_model(self.settings, "sonnet"), "claude-opus-5-5")

    def test_default_when_settings_missing_or_model_less(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ANTHROPIC_MODEL", None)
            self.assertEqual(claude_default_model(Path(self.tmp.name) / "nope.json", "sonnet"), "sonnet")
            self.settings.write_text(json.dumps({"model": 7}))
            self.assertIsNone(settings_claude_model(self.settings))
            self.assertEqual(claude_default_model(self.settings, "sonnet"), "sonnet")
            self.assertIsNone(claude_default_model(None))


if __name__ == "__main__":
    unittest.main()
