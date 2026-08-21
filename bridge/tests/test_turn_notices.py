"""Direct unit tests for core/turn_notices.py (#896 pure-move slice).

The composition rules were previously only exercised through
``TelegramBot`` (``test_session_start_notice.py``); this file pins them at
the function level so the delegators in ``bot.py`` can stay thin.
"""

import os
import unittest
from unittest import mock

from telegram_bot.core.turn_notices import (
    HISTORY_SNIPPET_CHARS,
    busy_notice_text,
    compose_history_injection,
    session_start_notice_text,
    session_start_reason,
)


class BusyNoticeTextTest(unittest.TestCase):
    def test_contains_elapsed_duration_and_promise(self):
        text = busy_notice_text(75.0)
        self.assertIn("Still working on the previous message", text)
        self.assertIn("elapsed", text)
        self.assertIn("after it finishes", text)

    def test_duration_is_humanized(self):
        self.assertNotEqual(busy_notice_text(30.0), busy_notice_text(3600.0))


class SessionStartReasonTest(unittest.TestCase):
    def test_precedence_auto_over_new_over_stale(self):
        self.assertEqual(
            session_start_reason(
                new_session=True, auto_new_session=True, stale_session_id="abc"
            ),
            "automatic reset",
        )
        self.assertEqual(
            session_start_reason(
                new_session=True, auto_new_session=False, stale_session_id="abc"
            ),
            "/new requested",
        )
        self.assertEqual(
            session_start_reason(
                new_session=False, auto_new_session=False, stale_session_id="abc"
            ),
            "previous session was not resumable",
        )
        self.assertEqual(
            session_start_reason(
                new_session=False, auto_new_session=False, stale_session_id=None
            ),
            "no active session",
        )


class SessionStartNoticeTextTest(unittest.TestCase):
    def _notice(self, **kwargs):
        kwargs.setdefault("reason", "automatic reset")
        kwargs.setdefault("model", None)
        return session_start_notice_text(**kwargs)

    def test_explicit_model_wins_over_env(self):
        env = {"CCC_MODEL_LABEL": "kimi k3", "ANTHROPIC_MODEL": "k3"}
        with mock.patch.dict(os.environ, env):
            self.assertIn("◆ Model: opus", self._notice(model="opus"))

    def test_model_label_env_fallback(self):
        env = {"CCC_MODEL_LABEL": "Fable 5 (high)"}
        with mock.patch.dict(os.environ, env, clear=False):
            self.assertIn("◆ Model: Fable 5 (high)", self._notice())

    def test_anthropic_model_only_for_claude(self):
        env = {"CCC_MODEL_LABEL": "", "ANTHROPIC_MODEL": "kimi-k3"}
        with mock.patch.dict(os.environ, env):
            self.assertIn("◆ Model: kimi-k3", self._notice(provider="claude"))
            self.assertIn("◆ Model: default", self._notice(provider="codex"))

    def test_unknown_provider_is_title_cased(self):
        with mock.patch.dict(os.environ, {"CCC_MODEL_LABEL": ""}):
            self.assertIn("◆ Provider: Zephyr", self._notice(provider="zephyr"))

    def test_previous_session_line_is_optional_and_truncated(self):
        with mock.patch.dict(os.environ, {"CCC_MODEL_LABEL": ""}):
            without = self._notice()
            with_prev = self._notice(previous_session_id="abcdef0123456789")
        self.assertNotIn("Previous session", without)
        self.assertIn("◆ Previous session: abcdef01… (not resumed)", with_prev)


class ComposeHistoryInjectionTest(unittest.TestCase):
    def test_empty_history_returns_text_unchanged(self):
        self.assertEqual(compose_history_injection([], "hello"), "hello")

    def test_labels_roles_and_wraps_current_message(self):
        recent = [
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "answer"},
        ]
        text = compose_history_injection(recent, "next")
        self.assertIn("사용자: question", text)
        self.assertIn("어시스턴트: answer", text)
        self.assertIn("[이전 대화 맥락 — 세션 전환으로 자동 주입됨]", text)
        self.assertTrue(text.endswith("[현재 메시지]\nnext"))

    def test_unknown_role_defaults_to_assistant_label(self):
        text = compose_history_injection([{"role": "tool", "content": "x"}], "t")
        self.assertIn("어시스턴트: x", text)

    def test_snippet_is_capped_and_newlines_collapsed(self):
        long_content = ("line1\nline2 " * 100).strip()
        text = compose_history_injection(
            [{"role": "user", "content": long_content}], "t"
        )
        body_line = text.split("\n")[1]
        self.assertLessEqual(len(body_line), len("사용자: ") + HISTORY_SNIPPET_CHARS)
        self.assertNotIn("line1\nline2", body_line)


class BotDelegatorCompatibilityTest(unittest.TestCase):
    """The class-level delegators must expose the same callables (#896)."""

    def test_bot_static_delegators_point_at_module_functions(self):
        from telegram_bot.core.bot import TelegramBot

        self.assertIs(
            TelegramBot._session_start_notice_text, session_start_notice_text
        )
        self.assertIs(TelegramBot._session_start_reason, session_start_reason)


if __name__ == "__main__":
    unittest.main()
