"""Tests for utils.tg_errors.is_not_modified."""

from telegram_bot.utils.tg_errors import is_not_modified

# The exact string Telegram returns for a no-op edit.
TELEGRAM_MSG = (
    "Message is not modified: specified new message content and reply markup "
    "are exactly the same as a current content and reply markup of the message"
)


def test_detects_exact_telegram_message():
    assert is_not_modified(Exception(TELEGRAM_MSG)) is True


def test_case_insensitive():
    assert is_not_modified(Exception("MESSAGE IS NOT MODIFIED")) is True


def test_plain_string():
    assert is_not_modified(TELEGRAM_MSG) is True


def test_none_is_false():
    assert is_not_modified(None) is False


def test_unrelated_error_is_false():
    assert is_not_modified(Exception("Bad Request: chat not found")) is False
    assert is_not_modified(Exception("Forbidden: bot was blocked by the user")) is False


if __name__ == "__main__":
    import sys
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok   - {name}")
            except AssertionError as e:
                failed += 1
                print(f"  FAIL - {name}: {e}")
    sys.exit(1 if failed else 0)
