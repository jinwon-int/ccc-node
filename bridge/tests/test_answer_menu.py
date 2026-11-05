"""#2676: bridge numbered menus answered by a bare number or the Hande card's `N. label` line."""

from __future__ import annotations

import pytest

from telegram_bot.core.answer_menu import ANSWER_NEEDED_ONE, MAX_CARD_ITEMS, menu_reply_number


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1", 1),
        (" 2 \n", 2),
        ("12", 12),
        ("123", 123),  # bare digits keep any length, as the menus accepted before
        ("1. 이어서 진행", 1),
        ("3) 상태만 확인", 3),
        ("  2.  Plan trip [claude]  ", 2),
        ("10. 💬 2 minutes ago hello", 10),
        ("1. A\r\n", 1),
    ],
)
def test_menu_answers(text: str, expected: int) -> None:
    assert menu_reply_number(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "", " ", "one", "1번", "1,", "1, 2", "1.", "1.A", "1. A\n2. B", "1. A\nplease", "123. A",
        "１", "x" * 401, None, 1,
    ],
)
def test_not_menu_answers(text: object) -> None:
    assert menu_reply_number(text) is None


def test_header_is_plain_and_single_choice() -> None:
    lower = ANSWER_NEEDED_ONE.lower()
    assert "[answer needed]" in lower and " one" in lower and "*" not in ANSWER_NEEDED_ONE
    assert MAX_CARD_ITEMS == 9
