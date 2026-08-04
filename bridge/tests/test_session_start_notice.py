"""Unit tests for the session-start notice model label fallback.

The banner previously rendered "◆ Model: default" whenever the session store
had no explicit /model choice, even when the runtime was routed to a specific
backend model via env (e.g. ANTHROPIC_MODEL on Kimi-routed nodes). The label
now falls back: session model → CCC_MODEL_LABEL → ANTHROPIC_MODEL (Claude
path only) → CCC_CRUSH_MODEL (crush path only) → "default". The provider
label maps claude/codex/crush instead of rendering every non-Claude
provider as "Codex".
"""

import os
import unittest
from unittest import mock

from telegram_bot.core.bot import TelegramBot


def _notice(**kwargs):
    kwargs.setdefault("reason", "automatic reset")
    kwargs.setdefault("model", None)
    return TelegramBot._session_start_notice_text(**kwargs)


class SessionStartNoticeModelTest(unittest.TestCase):
    def test_session_model_wins_over_env(self):
        env = {"CCC_MODEL_LABEL": "kimi k3", "ANTHROPIC_MODEL": "k3"}
        with mock.patch.dict(os.environ, env):
            text = _notice(model="opus")
        self.assertIn("◆ Model: opus", text)

    def test_label_wins_over_env_model(self):
        env = {"CCC_MODEL_LABEL": "kimi k3", "ANTHROPIC_MODEL": "k3"}
        with mock.patch.dict(os.environ, env):
            text = _notice()
        self.assertIn("◆ Model: kimi k3", text)

    def test_env_model_used_when_session_model_missing(self):
        env = {"CCC_MODEL_LABEL": "", "ANTHROPIC_MODEL": "k3"}
        with mock.patch.dict(os.environ, env):
            text = _notice()
        self.assertIn("◆ Model: k3", text)

    def test_env_model_ignored_for_codex_provider(self):
        env = {"CCC_MODEL_LABEL": "", "ANTHROPIC_MODEL": "k3"}
        with mock.patch.dict(os.environ, env):
            text = _notice(provider="codex")
        self.assertIn("◆ Model: default", text)

    def test_label_still_applies_for_codex_provider(self):
        env = {"CCC_MODEL_LABEL": "gpt-x", "ANTHROPIC_MODEL": "k3"}
        with mock.patch.dict(os.environ, env):
            text = _notice(provider="codex")
        self.assertIn("◆ Model: gpt-x", text)

    def test_default_when_nothing_set(self):
        env = {"CCC_MODEL_LABEL": "", "ANTHROPIC_MODEL": ""}
        with mock.patch.dict(os.environ, env):
            text = _notice()
        self.assertIn("◆ Model: default", text)

    def test_crush_provider_label(self):
        text = _notice(provider="crush")
        self.assertIn("fresh Crush stream", text)
        self.assertIn("◆ Provider: Crush", text)

    def test_codex_provider_label(self):
        text = _notice(provider="codex")
        self.assertIn("◆ Provider: Codex", text)

    def test_crush_model_env_used_for_crush_provider(self):
        env = {"CCC_MODEL_LABEL": "", "ANTHROPIC_MODEL": "k3", "CCC_CRUSH_MODEL": "kimi/k3"}
        with mock.patch.dict(os.environ, env):
            text = _notice(provider="crush")
        self.assertIn("◆ Model: kimi/k3", text)

    def test_anthropic_model_ignored_for_crush_provider(self):
        env = {"CCC_MODEL_LABEL": "", "ANTHROPIC_MODEL": "k3", "CCC_CRUSH_MODEL": ""}
        with mock.patch.dict(os.environ, env):
            text = _notice(provider="crush")
        self.assertIn("◆ Model: default", text)


if __name__ == "__main__":
    unittest.main()
