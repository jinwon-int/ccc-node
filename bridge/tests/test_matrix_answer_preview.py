"""Live answer preview in the Matrix progress bubble (#1796, first stage; off by default)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from telegram_bot.core.agent_runtime import (
    CompletionEvent,
    MessageCompletedEvent,
    TextDeltaEvent,
    ToolStartedEvent,
)
from telegram_bot.core.matrix.streaming import (
    DEFAULT_INTERVAL_S,
    PREVIEW_MAX_CHARS,
    MatrixAnswerPreview,
    interval_from,
)
from telegram_bot.core.streaming_sink import StreamingSinkPort
from test_matrix_bot import DM_ROOM, FakeSink, _bot, matrix_config  # noqa: F401 - fixture
from test_matrix_frontend_seams import _Runtime, _handler

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class Interims:
    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[str] = []
        self.fail = fail

    async def __call__(self, text: str) -> None:
        if self.fail:
            raise RuntimeError("send failed")
        self.sent.append(text)


def _preview(**kwargs: Any) -> tuple[MatrixAnswerPreview, FakeSink, Interims, Clock]:
    sink, interims, clock = FakeSink(), Interims(fail=kwargs.pop("fail", False)), Clock()
    preview = MatrixAnswerPreview(sink, interims, interval_s=kwargs.pop("interval_s", 2.0), clock=clock)
    return preview, sink, interims, clock


# --- unit ---------------------------------------------------------------------------


async def test_it_is_a_streaming_sink_that_never_claims_delivery() -> None:
    preview, sink, _interims, _clock = _preview()
    assert isinstance(preview, StreamingSinkPort)
    assert await preview.update_if_needed("hello") is False
    assert await preview.add_tool_call("shell", {"cmd": "cat ~/.ssh/id_ed25519"}) is False
    assert await preview.finalize_all() is False, "the final answer still goes through the outbox"
    assert sink.statuses[-1] is None, "the preview clears its own bubble"


async def test_updates_are_throttled_and_show_the_growing_text() -> None:
    preview, sink, _interims, clock = _preview()
    await preview.update_if_needed("안녕")
    await preview.update_if_needed("하세요")  # within the interval: no edit
    assert sink.statuses == ["안녕 ▍"]
    clock.now += 2.0
    await preview.update_if_needed("!")
    assert sink.statuses == ["안녕 ▍", "안녕하세요! ▍"]
    assert preview.showing


async def test_tool_lines_show_names_only_and_the_tail_of_long_text() -> None:
    preview, sink, _interims, clock = _preview()
    await preview.add_tool_call("shell", {"cmd": "echo SECRET"})
    clock.now += 5
    await preview.update_if_needed("x" * (PREVIEW_MAX_CHARS + 500))
    text = sink.statuses[-1]
    assert text.startswith("🔧 shell\n…") and "SECRET" not in text
    assert len(text) < PREVIEW_MAX_CHARS + 40


async def test_a_completed_intermediate_message_is_delivered_durably_and_the_bubble_cleared() -> None:
    preview, sink, interims, clock = _preview()
    await preview.update_if_needed("\x1b[31mfirst\x1b[0m part ")
    assert await preview.finalize_segment() is True
    assert interims.sent == ["first part"], "cleaned like the handler's _clean_response"
    assert sink.statuses[-1] is None and not preview.showing
    assert await preview.finalize_segment() is False, "nothing pending"


async def test_a_failed_interim_is_kept_for_the_final_reply() -> None:
    preview, _sink, _interims, _clock = _preview(fail=True)
    await preview.update_if_needed("part")
    assert await preview.finalize_segment() is False


async def test_a_failing_bubble_never_fails_the_turn() -> None:
    preview, sink, _interims, _clock = _preview()

    async def broken(text: Any) -> None:
        raise RuntimeError("homeserver down")

    sink.status = broken  # type: ignore[method-assign]
    assert await preview.update_if_needed("x") is False
    assert not preview.showing
    assert await preview.cancel() is False


def test_the_interval_is_clamped() -> None:
    assert interval_from("") == DEFAULT_INTERVAL_S
    assert interval_from("nan") == DEFAULT_INTERVAL_S
    assert interval_from("0.1") == 1.0
    assert interval_from("999") == 60.0


# --- through the real ProjectChatHandler -------------------------------------------------


async def test_the_real_handler_keeps_the_final_reply_and_delivers_intermediate_messages(tmp_path: Path) -> None:
    runtime = _Runtime(
        [
            TextDeltaEvent("Let me check."),
            MessageCompletedEvent(),
            ToolStartedEvent("t1", "shell", {"cmd": "ls"}),
            TextDeltaEvent("Done: 3 files."),
            CompletionEvent("end_turn"),
        ]
    )
    preview, sink, interims, _clock = _preview()
    response = await _handler(tmp_path, runtime).process_message(
        "hi", user_id=7, chat_id=7, streaming_sink=preview
    )
    assert response.success and not response.streamed, "the outbox still delivers the answer"
    assert response.content == "Done: 3 files."
    assert interims.sent == ["Let me check."], "the intermediate message went out on its own"
    assert sink.statuses and sink.statuses[-1] is None, "no preview is left behind"


# --- MatrixBot wiring ------------------------------------------------------------------


@pytest.mark.usefixtures("matrix_config")
def test_streaming_is_off_by_default(tmp_path: Path) -> None:
    bot, _chat, _manager = _bot(tmp_path)
    callbacks = bot._progress_callbacks(FakeSink(), DM_ROOM)
    assert "streaming_sink" not in callbacks
    assert set(callbacks) == {"status_callback", "interim_message_callback"}


@pytest.mark.usefixtures("matrix_config")
async def test_streaming_on_passes_the_preview_and_holds_back_heartbeats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CCC_MATRIX_STREAMING", "1")
    monkeypatch.setenv("CCC_MATRIX_DRAFT_EDIT_INTERVAL_S", "1")
    bot, _chat, _manager = _bot(tmp_path)
    sink = FakeSink()
    callbacks = bot._progress_callbacks(sink, DM_ROOM)
    preview = callbacks["streaming_sink"]
    assert isinstance(preview, MatrixAnswerPreview)
    await preview.update_if_needed("answer so far")
    assert sink.statuses == ["answer so far ▍"]
    handle = await callbacks["status_callback"]("⏳ Working 1m", None)
    assert handle is not None, "the heartbeat handle keeps cleanup armed"
    assert sink.statuses == ["answer so far ▍"], "the heartbeat did not overwrite the preview"


@pytest.mark.usefixtures("matrix_config")
async def test_process_message_gets_the_preview_only_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_matrix_bot import _job, _resume_existing_session

    bot, chat, manager = _bot(tmp_path)
    _resume_existing_session(bot, manager)
    await bot.run_turn(_job("hi"), sink=FakeSink(), session_id=None, room_kind="direct")
    assert "streaming_sink" not in chat.calls[-1]
    monkeypatch.setenv("CCC_MATRIX_STREAMING", "true")
    await bot.run_turn(_job("again", event_id="$e2"), sink=FakeSink(), session_id=None, room_kind="direct")
    assert isinstance(chat.calls[-1]["streaming_sink"], MatrixAnswerPreview)
