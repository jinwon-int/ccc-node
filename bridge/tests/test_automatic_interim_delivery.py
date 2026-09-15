"""Automatic turns must expose completed messages before a blocked tool finishes."""

import asyncio
from types import SimpleNamespace

import pytest

from test_project_chat_codex import FakeRuntime, FakeSession, _handler
from telegram_bot.core import bot_delivery, bot_lifecycle
from telegram_bot.core.agent_runtime import (
    CompletionEvent,
    ErrorEvent,
    MessageCompletedEvent,
    ResultEvent,
    TextDeltaEvent,
    ToolCompletedEvent,
    ToolStartedEvent,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


class Lifecycle(bot_lifecycle.BotLifecycleMixin, bot_delivery.BotDeliveryMixin):
    def _require_application(self):
        return self.application


@pytest.mark.anyio
@pytest.mark.parametrize("lane", ["continuation", "ci_resume"])
@pytest.mark.parametrize("outcome", ["final", "interim_only", "interim_send_failure", "final_send_failure", "runtime_failure", "cancel"])
async def test_automatic_interim_delivery(tmp_path, monkeypatch, lane, outcome):
    blocked = asyncio.Event()
    release = asyncio.Event()
    sent = []
    calls = []
    attempts = []

    class Session(FakeSession):
        def send_turn(self, message, **kwargs):
            async def events():
                yield TextDeltaEvent("Progress report")
                yield MessageCompletedEvent()
                yield ToolStartedEvent("tool1", "command", {})
                blocked.set()
                await release.wait()
                yield ToolCompletedEvent("tool1", "command", {}, True)
                if outcome == "runtime_failure":
                    yield ErrorEvent("fixture_failure", "Synthetic failure", False)
                elif outcome != "interim_only":
                    yield TextDeltaEvent("Final answer")
                    yield MessageCompletedEvent()
                yield ResultEvent({"status": "completed"})
                yield CompletionEvent("end_turn")
            return events()

    class Bot:
        async def send_message(self, chat_id, text, **kwargs):
            assert chat_id == 70
            attempts.append(text)
            if outcome == "interim_send_failure" and not release.is_set():
                raise OSError("synthetic pre-send failure")
            if outcome == "final_send_failure" and release.is_set():
                raise OSError("synthetic pre-send failure")
            sent.append(text)

    lifecycle = Lifecycle()
    lifecycle._config = SimpleNamespace(
        bot_data_dir=tmp_path,
        project_root=str(tmp_path),
        telegram_max_bubble_chars=1200,
        enable_entity_renderer=True,
    )
    lifecycle.application = SimpleNamespace(bot=Bot())

    async def lookup(user_id):
        assert user_id == 7
        return {"session_id": "canonical-session"}

    lifecycle._session_manager = SimpleNamespace(get_session=lookup)
    real = _handler(tmp_path, FakeRuntime([Session("canonical-session")]))

    class Forwarder:
        async def process_message(self, *args, **kwargs):
            calls.append(kwargs)
            return await real.process_message(*args, **kwargs)

    lifecycle._project_chat = Forwarder()
    monkeypatch.setenv("CCC_CONTINUATION_ENABLED", "1")
    monkeypatch.setenv("CCC_EXTERNAL_WAIT_ENABLED", "1")
    record = {"user_id": 7, "chat_id": 70, "session_id": "old-session",
              "wait_id": "fixture", "continuation_id": "fixture"}
    monitor = (lifecycle._build_continuation_monitor() if lane == "continuation"
               else lifecycle._build_external_wait_monitor())
    runner = monitor._runner if lane == "continuation" else monitor._resumer
    task = asyncio.create_task(runner(record, "synthetic prompt"))
    try:
        await asyncio.wait_for(blocked.wait(), 5)
        assert not task.done()
        assert sent == ([] if outcome == "interim_send_failure" else ["Progress report"])
        assert calls[0]["session_id"] == "canonical-session"
        assert calls[0]["usage_mode"] == "autonomous"
        assert calls[0]["notification_bot"] is lifecycle.application.bot
        assert calls[0].get("bot") is None  # Completed messages, no live draft.
        if outcome == "cancel":
            task.cancel()
    finally:
        release.set()
        if not blocked.is_set():
            task.cancel()
    if outcome == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await task
        assert sent == ["Progress report"]
        return
    result = await asyncio.wait_for(task, 5)
    _assert_delivery_result(outcome, result, sent, attempts)


def _assert_delivery_result(outcome, result, sent, attempts):
    if outcome in {"runtime_failure", "final_send_failure"}:
        assert result is False
        assert sent == ["Progress report"]
        if outcome == "final_send_failure":
            assert attempts.count("Final answer") == 2  # Existing final retry.
    else:
        assert result is True
        if outcome == "interim_send_failure":
            assert sent == ["Progress report\n\nFinal answer"]
        elif outcome == "interim_only":
            assert sent == ["Progress report"]
        else:
            assert sent == ["Progress report", "Final answer"]
