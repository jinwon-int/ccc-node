"""Unit contract for the typed project-chat turn output state (#346)."""

from telegram_bot.core.project_chat_output import TurnOutputBuffer


def _clean(text: str) -> str:
    return " ".join(text.split())


def test_terminal_completed_message_renders_once() -> None:
    output = TurnOutputBuffer()

    output.append_delta(" final ")
    output.append_delta(" answer ")
    output.complete_message(_clean)

    assert output.has_text
    assert output.render(_clean) == "final answer"
    assert not output.interim_delivered


def test_successful_interim_is_not_repeated_in_final_render() -> None:
    output = TurnOutputBuffer()
    output.append_delta("first")
    output.complete_message(_clean)

    interim = output.pending_interim
    assert interim == "first"
    output.resolve_pending_interim(delivered=True)
    output.append_delta("second")

    assert output.interim_delivered
    assert output.render(_clean) == "second"
    assert output.pending_interim is None


def test_failed_interim_is_retained_before_later_output() -> None:
    output = TurnOutputBuffer()
    output.append_delta("first")
    output.complete_message(_clean)

    interim = output.pending_interim
    assert interim == "first"
    output.resolve_pending_interim(delivered=False)
    output.append_delta("second")
    output.complete_message(_clean)

    assert output.render(_clean) == "first\n\nsecond"
    assert not output.interim_delivered


def test_retained_pending_and_current_preserve_order_without_duplicates() -> None:
    output = TurnOutputBuffer()
    output.append_delta("retained")
    output.complete_message(_clean)
    retained = output.pending_interim
    assert retained is not None
    output.resolve_pending_interim(delivered=False)

    output.append_delta("pending")
    output.complete_message(_clean)
    output.append_delta("current")

    assert output.render(_clean) == "retained\n\npending\n\ncurrent"
    assert output.render(_clean) == "retained\n\npending\n\ncurrent"


def test_empty_delta_and_cleaned_empty_completion_are_noise() -> None:
    output = TurnOutputBuffer()

    output.append_delta("")
    output.append_delta(" \n ")
    output.complete_message(_clean)

    assert output.has_text
    assert output.pending_interim is None
    assert output.render(_clean) == ""


def test_pending_peek_survives_unresolved_delivery() -> None:
    output = TurnOutputBuffer()
    output.append_delta("one")
    output.complete_message(_clean)

    assert output.pending_interim == "one"
    assert output.pending_interim == "one"
    assert output.render(_clean) == "one"


def test_pending_resolution_is_one_shot() -> None:
    output = TurnOutputBuffer()
    output.append_delta("one")
    output.complete_message(_clean)

    output.resolve_pending_interim(delivered=False)
    output.resolve_pending_interim(delivered=False)

    assert output.pending_interim is None
    assert output.render(_clean) == "one"
