"""Telegram error classification helpers (no PTB import needed)."""


def is_not_modified(error) -> bool:
    """True for Telegram's harmless "message is not modified" 400.

    Telegram rejects ``editMessageText`` / ``editMessageReplyMarkup`` calls whose
    new text + reply markup are byte-identical to the current message. The edit
    was a no-op, so the message already shows the intended content — callers
    should swallow this rather than surface it as an error.
    """
    return error is not None and "message is not modified" in str(error).lower()
