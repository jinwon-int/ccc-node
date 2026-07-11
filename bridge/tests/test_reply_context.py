"""Unit tests for the Telegram reply-context prefix helper.

``build_reply_context_prefix`` turns an inbound reply's quoted original into a
``[Replying to: "..."]`` prefix the message handlers prepend before the user's
text, so the agent knows which prior message a reply refers to. Before this the
bridge forwarded only the new text and the quoted original was dropped.

Telegram objects are mocked with ``SimpleNamespace`` to keep these tests pure —
no bot, no network.
"""

import unittest
from types import SimpleNamespace

from telegram_bot.core.bot_shared import (
    REPLY_CONTEXT_MAX_LEN,
    build_reply_context_prefix,
)


def _msg(*, reply_to=None, quote=None):
    return SimpleNamespace(reply_to_message=reply_to, quote=quote)


def _reply(*, text=None, caption=None, from_user=None):
    return SimpleNamespace(
        message_id=111,
        text=text,
        caption=caption,
        from_user=from_user,
    )


def _user(user_id=None, is_bot=False):
    return SimpleNamespace(id=user_id, is_bot=is_bot)


class BuildReplyContextPrefixTest(unittest.TestCase):
    def test_not_a_reply_returns_none(self):
        self.assertIsNone(build_reply_context_prefix(_msg()))

    def test_reply_to_text_message(self):
        msg = _msg(reply_to=_reply(text="deploy the staging box", from_user=_user(7, False)))
        prefix = build_reply_context_prefix(msg, bot_user_id=99)
        self.assertEqual(prefix, '[Replying to: "deploy the staging box"]')

    def test_native_partial_quote_preferred_over_full_text(self):
        # User selected only a substring of a multi-section message.
        msg = _msg(
            reply_to=_reply(
                text="line one\nDO THE DANGEROUS THING\nline three",
                from_user=_user(7, False),
            ),
            quote=SimpleNamespace(text="line one"),
        )
        prefix = build_reply_context_prefix(msg, bot_user_id=99)
        self.assertEqual(prefix, '[Replying to: "line one"]')

    def test_caption_fallback_when_no_text(self):
        msg = _msg(reply_to=_reply(text=None, caption="chart.png overview", from_user=_user(7)))
        prefix = build_reply_context_prefix(msg, bot_user_id=99)
        self.assertEqual(prefix, '[Replying to: "chart.png overview"]')

    def test_empty_text_falls_through_to_caption(self):
        msg = _msg(reply_to=_reply(text="", caption="fallback caption", from_user=_user(7)))
        prefix = build_reply_context_prefix(msg, bot_user_id=99)
        self.assertEqual(prefix, '[Replying to: "fallback caption"]')

    def test_own_message_by_bot_id(self):
        msg = _msg(reply_to=_reply(text="CI is green.", from_user=_user(99, is_bot=True)))
        prefix = build_reply_context_prefix(msg, bot_user_id=99)
        self.assertEqual(prefix, '[Replying to your previous message: "CI is green."]')

    def test_own_message_by_is_bot_fallback_when_no_bot_id(self):
        msg = _msg(reply_to=_reply(text="CI is green.", from_user=_user(99, is_bot=True)))
        prefix = build_reply_context_prefix(msg, bot_user_id=None)
        self.assertEqual(prefix, '[Replying to your previous message: "CI is green."]')

    def test_other_bot_is_not_own_when_bot_id_differs(self):
        # A different bot replied-to (is_bot True) but id != our bot id → not "own".
        msg = _msg(reply_to=_reply(text="hi from another bot", from_user=_user(12345, is_bot=True)))
        prefix = build_reply_context_prefix(msg, bot_user_id=99)
        self.assertEqual(prefix, '[Replying to: "hi from another bot"]')

    def test_media_only_reply_returns_none(self):
        # Sticker/photo with no text and no caption → nothing to inject.
        msg = _msg(reply_to=_reply(text=None, caption=None, from_user=_user(7)))
        self.assertIsNone(build_reply_context_prefix(msg, bot_user_id=99))

    def test_whitespace_only_snippet_returns_none(self):
        msg = _msg(reply_to=_reply(text="   \n  ", from_user=_user(7)))
        self.assertIsNone(build_reply_context_prefix(msg, bot_user_id=99))

    def test_snippet_is_truncated(self):
        long_text = "x" * (REPLY_CONTEXT_MAX_LEN + 250)
        msg = _msg(reply_to=_reply(text=long_text, from_user=_user(7)))
        prefix = build_reply_context_prefix(msg, bot_user_id=99)
        # Prefix wrapper is [Replying to: "<snippet>"] — inner snippet capped.
        inner = prefix[len('[Replying to: "'):-len('"]')]
        self.assertEqual(len(inner), REPLY_CONTEXT_MAX_LEN)

    def test_snippet_is_stripped(self):
        msg = _msg(reply_to=_reply(text="  padded  ", from_user=_user(7)))
        prefix = build_reply_context_prefix(msg, bot_user_id=99)
        self.assertEqual(prefix, '[Replying to: "padded"]')

    def test_missing_from_user_is_not_own(self):
        msg = _msg(reply_to=_reply(text="orphan reply", from_user=None))
        prefix = build_reply_context_prefix(msg, bot_user_id=99)
        self.assertEqual(prefix, '[Replying to: "orphan reply"]')

    def test_prefix_composes_cleanly_with_text(self):
        # The handlers do f"{prefix}\n\n{text}"; verify the prefix is single-line
        # and carries no surrounding newlines so composition is predictable.
        msg = _msg(reply_to=_reply(text="original", from_user=_user(7)))
        prefix = build_reply_context_prefix(msg, bot_user_id=99)
        composed = f"{prefix}\n\n{'my reply'}"
        self.assertEqual(composed, '[Replying to: "original"]\n\nmy reply')


if __name__ == "__main__":
    unittest.main()
