"""Numbered bridge menus that 「한데」 renders as a tap-to-choose card (#2676).

The bridge's own numbered menus (``/resume``, the two ``/revert`` steps, the
Danso recovery offer) are answered by number. Hande draws a choice card only
for a list the sender marked with an answer-needed header line directly above
item ``1`` (family-messenger ``AGENT-CHOICES-V1.md`` §5/§6), and a
single-choice card sends one ``N. label`` line. So every menu renders as::

    [Answer needed] choose one
    1. first label
    2. second label

and every menu answer accepts either a bare number (typed by hand, unchanged)
or exactly one ``N. label`` / ``N) label`` line (the card). Labels are not
compared, matching the card's own answer rule. Pure helpers, no bot state.
"""

from __future__ import annotations

import re
from typing import Optional

# No markdown: Telegram sends these menus as plain text, where ``**`` would show.
# Hande matches ``[answer needed]`` case-insensitively and reads the word
# ``one`` as single-choice mode (radio card, one line sent).
ANSWER_NEEDED_ONE = "[Answer needed] choose one"

# Hande cards hold at most nine items; longer menus stay plain numbered text.
MAX_CARD_ITEMS = 9

_BARE = re.compile(r"[0-9]+")  # any length, as the menus accepted before
_LINE = re.compile(r" {0,3}([0-9]{1,2})[.)][ \t]+\S[^\n]*")
_MAX_REPLY_CHARS = 400


def menu_reply_number(text: object) -> Optional[int]:
    """The chosen number for a menu answer, else ``None``.

    Accepts ``"2"`` (surrounding whitespace ignored) or one line ``"2. label"``
    (one or two digits, as Hande item lines). Several lines or extra prose are
    not menu answers; range checks stay with the caller.
    """

    if not isinstance(text, str) or len(text) > _MAX_REPLY_CHARS:
        return None
    body = text.replace("\r\n", "\n").strip(" \t\r\n")
    if _BARE.fullmatch(body):
        return int(body)
    match = _LINE.fullmatch(body)
    return int(match.group(1)) if match else None
