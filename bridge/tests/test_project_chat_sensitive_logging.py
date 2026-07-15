"""Sensitive-input logging contract for provider prompts with attachment metadata."""

from unittest.mock import patch

from telegram_bot.core import project_chat_process


def test_sensitive_input_logs_only_sanitized_event_and_skips_chat_log() -> None:
    with (
        patch.object(project_chat_process.logger, "info") as info,
        patch.object(project_chat_process, "log_chat") as log_chat,
    ):
        project_chat_process._log_user_input(
            user_message="private filename, caption, and local path",
            user_id=123456,
            session_id="private-session",
            model="private-model",
            sensitive_log_event="Inbound Document\nmetadata",
        )

    info.assert_called_once_with(
        "Processing sensitive input event=%s", "inbound_document_metadata"
    )
    log_chat.assert_not_called()
    rendered = repr(info.call_args)
    assert "private filename" not in rendered
    assert "123456" not in rendered
    assert "private-session" not in rendered
    assert "private-model" not in rendered


def test_normal_input_keeps_existing_chat_logging_behavior() -> None:
    with (
        patch.object(project_chat_process.logger, "info") as info,
        patch.object(project_chat_process, "log_chat") as log_chat,
    ):
        project_chat_process._log_user_input(
            user_message="normal message",
            user_id=7,
            session_id="session",
            model="model",
            sensitive_log_event=None,
        )

    info.assert_called_once_with(
        "Processing message from user %s: %s...", 7, "normal message"
    )
    log_chat.assert_called_once_with(7, "session", "user", "normal message", model="model")
