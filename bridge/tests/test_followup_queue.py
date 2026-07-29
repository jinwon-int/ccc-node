"""Regression coverage for #784 proposal 2 durable follow-up queuing."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telegram import Bot, Update

from telegram_bot.core.bot import TelegramBot, _FollowupUpdateEnvelope
from telegram_bot.core.bot_lifecycle import BotLifecycleMixin
from telegram_bot.core.project_chat_state import PersistentFollowupQueue
from telegram_bot.core.task_queue import UserTaskQueue


def _update_payload(text: str, *, update_id: int = 1) -> dict[str, object]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 1,
            "chat": {"id": 70, "type": "private"},
            "from": {
                "id": 7,
                "is_bot": False,
                "first_name": "Owner",
            },
            "text": text,
        },
    }


class _SerializableUpdate:
    def __init__(self, text: str, *, update_id: int = 1) -> None:
        self.payload = _update_payload(text, update_id=update_id)
        self.message = SimpleNamespace(reply_text=AsyncMock())
        self.effective_user = SimpleNamespace(id=7)
        self.effective_chat = SimpleNamespace(id=70)

    def to_json(self) -> str:
        return json.dumps(self.payload)


def _queue(path: Path, *, cap: int = 32) -> PersistentFollowupQueue:
    queue = PersistentFollowupQueue(path, per_chat_cap=cap)
    queue.initialize()
    return queue


def _bot_harness(path: Path, *, cap: int = 32) -> TelegramBot:
    bot = TelegramBot.__new__(TelegramBot)
    bot._config = SimpleNamespace(
        busy_notice_enabled=True,
        busy_notice_min_elapsed_seconds=10.0,
    )
    bot._project_chat = SimpleNamespace(
        busy_for_seconds=lambda user_id, chat_id, now: 120.0
    )
    bot._followup_queue = _queue(path, cap=cap)
    bot._followup_admission_locks = {}
    bot._followup_idle_events = {}
    bot._followup_live_counts = {}
    bot._followup_workers = {}
    bot._followup_workers_stopping = False
    bot._followup_queue_enabled = True
    bot._tasks = UserTaskQueue(3)
    bot.application = SimpleNamespace(
        bot=SimpleNamespace(send_message=AsyncMock())
    )
    return bot


async def _wait_until(predicate, *, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("condition was not reached")
        await asyncio.sleep(0)


def test_persistent_followup_queue_preserves_fifo_and_cap_across_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "followup-queue.json"
    queue = _queue(path, cap=3)
    key = '["str","7:70"]'

    accepted = []
    for index, text in enumerate(("one", "two", "three"), start=1):
        item, position = queue.enqueue(
            conversation_key=key,
            handler="text",
            update_json=json.dumps(_update_payload(text, update_id=index)),
            enqueued_at=float(index),
        )
        assert item is not None
        accepted.append(item)
        assert position == index

    rejected, position = queue.enqueue(
        conversation_key=key,
        handler="text",
        update_json=json.dumps(_update_payload("four", update_id=4)),
        enqueued_at=4.0,
    )
    assert rejected is None
    assert position == 3

    restarted = _queue(path, cap=3)
    observed = []
    while (item := restarted.peek(key)) is not None:
        observed.append(json.loads(item.update_json)["message"]["text"])
        assert restarted.acknowledge(item.item_id)

    assert observed == ["one", "two", "three"]
    assert path.stat().st_mode & 0o777 == 0o600
    assert [item.item_id for item in accepted] == [1, 2, 3]


@pytest.mark.anyio
async def test_queued_followups_process_fifo_after_active_turn_releases(
    tmp_path: Path,
) -> None:
    bot = _bot_harness(tmp_path / "queue.json")
    queue_key = bot._followup_queue_key("7:70")
    bot._mark_followup_live(queue_key)
    processed: list[str] = []

    async def dispatch(item) -> None:
        processed.append(json.loads(item.update_json)["message"]["text"])

    bot._dispatch_queued_followup = dispatch
    updates = [
        _SerializableUpdate(text, update_id=index)
        for index, text in enumerate(("one", "two", "three"), start=1)
    ]
    for update in updates:
        assert await bot._persist_followup(
            queue_key=queue_key,
            envelope=_FollowupUpdateEnvelope("text", update),
            busy_seconds=120.0,
        )

    assert processed == []
    receipt = updates[0].message.reply_text.await_args.args[0]
    assert "saved in queue position 1" in receipt
    assert "dropped" not in receipt

    bot._unmark_followup_live(queue_key)
    await _wait_until(lambda: bot._followup_queue.depth(queue_key) == 0)
    assert processed == ["one", "two", "three"]
    await bot._stop_followup_workers()


@pytest.mark.anyio
async def test_failed_head_retries_are_persisted_then_tail_drains(
    tmp_path: Path,
) -> None:
    bot = _bot_harness(tmp_path / "queue.json")
    queue_key = bot._followup_queue_key("7:70")
    attempts: list[tuple[str, int]] = []
    processed: list[str] = []

    async def dispatch(item) -> None:
        text = json.loads(item.update_json)["message"]["text"]
        attempts.append((text, item.retry_count))
        if text == "poison":
            raise RuntimeError("confirmed dispatch failure")
        processed.append(text)

    bot._dispatch_queued_followup = dispatch
    for index, text in enumerate(("poison", "two", "three"), start=1):
        item, _ = bot._followup_queue.enqueue(
            conversation_key=queue_key,
            handler="text",
            update_json=json.dumps(_update_payload(text, update_id=index)),
            enqueued_at=float(index),
        )
        assert item is not None

    bot._start_followup_worker(queue_key)
    await _wait_until(lambda: bot._followup_queue.depth(queue_key) == 0)
    await _wait_until(lambda: queue_key not in bot._followup_workers)

    assert [retry for text, retry in attempts if text == "poison"] == [0, 1, 2]
    assert processed == ["two", "three"]
    notice = bot.application.bot.send_message.await_args.kwargs
    assert notice["reply_to_message_id"] == 1
    assert "could not be processed after 3 attempts" in notice["text"]
    assert "Later follow-ups will continue" in notice["text"]
    await bot._stop_followup_workers()


@pytest.mark.anyio
async def test_cap_exceeded_is_explicit_and_never_silent(tmp_path: Path) -> None:
    bot = _bot_harness(tmp_path / "queue.json", cap=1)
    queue_key = bot._followup_queue_key("7:70")
    bot._mark_followup_live(queue_key)
    first = _SerializableUpdate("one", update_id=1)
    second = _SerializableUpdate("two", update_id=2)

    assert await bot._persist_followup(
        queue_key=queue_key,
        envelope=_FollowupUpdateEnvelope("text", first),
        busy_seconds=120.0,
    )
    assert not await bot._persist_followup(
        queue_key=queue_key,
        envelope=_FollowupUpdateEnvelope("text", second),
        busy_seconds=120.0,
    )

    reply = second.message.reply_text.await_args.args[0]
    assert "queue is full (1 messages)" in reply
    assert "This message was not queued" in reply
    assert bot._followup_queue.depth(queue_key) == 1
    await bot._stop_followup_workers()


@pytest.mark.anyio
async def test_occupied_enqueue_seam_persists_instead_of_running_in_memory(
    tmp_path: Path,
) -> None:
    bot = _bot_harness(tmp_path / "queue.json")
    conversation_key = "7:70"
    queue_key = bot._followup_queue_key(conversation_key)
    bot._mark_followup_live(queue_key)
    bot._dispatch_queued_followup = AsyncMock()
    update = _SerializableUpdate("persist me")
    run_task = AsyncMock()
    on_overflow = AsyncMock()

    async def enter_enqueue(_update, _context) -> None:
        assert await bot._enqueue_user_task(
            conversation_key,
            run_task,
            on_overflow,
        )

    await bot._with_followup_update(
        handler="text",
        update=update,
        context=None,
        callback=enter_enqueue,
    )

    run_task.assert_not_awaited()
    on_overflow.assert_not_awaited()
    assert bot._followup_queue.depth(queue_key) == 1
    await bot._stop_followup_workers()


@pytest.mark.anyio
async def test_stop_then_immediate_followup_rearms_worker(tmp_path: Path) -> None:
    bot = _bot_harness(tmp_path / "queue.json")
    queue_key = bot._followup_queue_key("7:70")
    bot._mark_followup_live(queue_key)
    processed: list[str] = []

    async def dispatch(item) -> None:
        processed.append(json.loads(item.update_json)["message"]["text"])

    bot._dispatch_queued_followup = dispatch
    assert await bot._persist_followup(
        queue_key=queue_key,
        envelope=_FollowupUpdateEnvelope("text", _SerializableUpdate("old")),
        busy_seconds=120.0,
    )

    volatile_cleared, durable_cleared = await bot._clear_user_queue("7:70")
    assert (volatile_cleared, durable_cleared) == (0, 1)
    assert await bot._persist_followup(
        queue_key=queue_key,
        envelope=_FollowupUpdateEnvelope("text", _SerializableUpdate("new")),
        busy_seconds=120.0,
    )
    assert queue_key in bot._followup_workers
    assert not bot._followup_workers[queue_key].done()

    bot._unmark_followup_live(queue_key)
    await _wait_until(lambda: bot._followup_queue.depth(queue_key) == 0)
    assert processed == ["new"]
    await bot._stop_followup_workers()


@pytest.mark.anyio
async def test_shutdown_retains_item_and_restart_drains_it(tmp_path: Path) -> None:
    path = tmp_path / "queue.json"
    first_bot = _bot_harness(path)
    queue_key = first_bot._followup_queue_key("7:70")
    first_bot._mark_followup_live(queue_key)
    update = _SerializableUpdate("survives restart")

    assert await first_bot._persist_followup(
        queue_key=queue_key,
        envelope=_FollowupUpdateEnvelope("text", update),
        busy_seconds=120.0,
    )
    await first_bot._stop_followup_workers()
    assert first_bot._followup_queue.depth(queue_key) == 1

    restarted_bot = _bot_harness(path)
    processed: list[str] = []

    async def dispatch(item) -> None:
        processed.append(json.loads(item.update_json)["message"]["text"])

    restarted_bot._dispatch_queued_followup = dispatch
    restarted_bot._start_followup_worker(queue_key)
    await _wait_until(lambda: restarted_bot._followup_queue.depth(queue_key) == 0)

    assert processed == ["survives restart"]
    await restarted_bot._stop_followup_workers()


@pytest.mark.anyio
async def test_corrupt_queue_degrades_without_stopping_bridge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "queue.json"
    bot = _bot_harness(path)
    original = b"{confirmed-corruption"
    path.write_bytes(original)
    path.chmod(0o600)
    lifecycle_ready = AsyncMock()
    monkeypatch.setattr(BotLifecycleMixin, "_on_ready", lifecycle_ready)

    await bot._on_ready(SimpleNamespace())

    lifecycle_ready.assert_awaited_once()
    assert bot._followup_queue_enabled is False
    assert bot._followup_workers == {}
    assert path.read_bytes() == original


@pytest.mark.anyio
async def test_replay_rechecks_live_access_before_any_processing(
    tmp_path: Path,
) -> None:
    bot = _bot_harness(tmp_path / "queue.json")
    bot._check_access = AsyncMock(return_value=False)
    bot._enqueue_user_task = AsyncMock()
    item, _ = bot._followup_queue.enqueue(
        conversation_key=bot._followup_queue_key("7:70"),
        handler="text",
        update_json=json.dumps(_update_payload("authorize me at replay")),
        enqueued_at=time.time(),
    )
    assert item is not None

    await bot._dispatch_queued_followup(item)

    bot._check_access.assert_awaited_once()
    replayed = bot._check_access.await_args.args[0]
    assert replayed.effective_user.id == 7
    assert replayed.effective_chat.id == 70
    assert replayed.message.text == "authorize me at replay"
    assert replayed.message.date.timestamp() > time.time() - 5
    bot._enqueue_user_task.assert_not_awaited()


@pytest.mark.anyio
async def test_de_json_none_is_retried_discarded_and_does_not_stall_tail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = _bot_harness(tmp_path / "queue.json")
    queue_key = bot._followup_queue_key("7:70")
    original_de_json = Update.de_json
    poison_calls = 0

    def deserialize(payload, telegram_bot):
        nonlocal poison_calls
        if payload["message"].get("text") == "poison":
            poison_calls += 1
            return None
        return original_de_json(payload, telegram_bot)

    monkeypatch.setattr(Update, "de_json", staticmethod(deserialize))
    bot._handle_text_message = AsyncMock()
    for index, text in enumerate(("poison", "tail"), start=1):
        item, _ = bot._followup_queue.enqueue(
            conversation_key=queue_key,
            handler="text",
            update_json=json.dumps(_update_payload(text, update_id=index)),
            enqueued_at=float(index),
        )
        assert item is not None

    bot._start_followup_worker(queue_key)
    await _wait_until(lambda: bot._followup_queue.depth(queue_key) == 0)

    assert poison_calls == bot._FOLLOWUP_MAX_ATTEMPTS
    bot._handle_text_message.assert_awaited_once()
    replayed = bot._handle_text_message.await_args.args[0]
    assert replayed.message.text == "tail"
    assert "could not be processed" in (
        bot.application.bot.send_message.await_args.kwargs["text"]
    )
    await bot._stop_followup_workers()


@pytest.mark.anyio
async def test_voice_update_round_trips_and_replays_through_voice_handler(
    tmp_path: Path,
) -> None:
    bot = _bot_harness(tmp_path / "queue.json")
    telegram_bot = Bot("123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk")
    bot.application = SimpleNamespace(bot=telegram_bot)
    payload = _update_payload("", update_id=9)
    message_payload = payload["message"]
    assert isinstance(message_payload, dict)
    message_payload.pop("text")
    message_payload["voice"] = {
        "file_id": "voice-id",
        "file_unique_id": "voice-unique-id",
        "duration": 2,
        "mime_type": "audio/ogg",
        "file_size": 42,
    }
    live_update = Update.de_json(payload, telegram_bot)
    assert live_update is not None
    bot._handle_voice_message = AsyncMock()
    item, _ = bot._followup_queue.enqueue(
        conversation_key=bot._followup_queue_key("7:70"),
        handler="voice",
        update_json=bot._serialize_followup_update(live_update),
        enqueued_at=time.time(),
    )
    assert item is not None

    await bot._dispatch_queued_followup(item)

    bot._handle_voice_message.assert_awaited_once()
    replayed = bot._handle_voice_message.await_args.args[0]
    assert replayed.message.voice.file_id == "voice-id"
    replayed_json = json.loads(replayed.to_json())
    live_json = json.loads(live_update.to_json())
    replayed_json["message"].pop("date")
    live_json["message"].pop("date")
    assert replayed_json == live_json


@pytest.mark.anyio
async def test_stop_reply_reports_discarded_durable_count(tmp_path: Path) -> None:
    bot = _bot_harness(tmp_path / "queue.json")
    queue_key = bot._followup_queue_key("7:70")
    for index in (1, 2):
        item, _ = bot._followup_queue.enqueue(
            conversation_key=queue_key,
            handler="text",
            update_json=json.dumps(_update_payload(str(index), update_id=index)),
            enqueued_at=float(index),
        )
        assert item is not None
    bot._check_access = AsyncMock(return_value=True)
    bot._deny_codex_approvals = Mock()
    bot._invalidate_codex_approvals = Mock()
    bot._cancel_user_voice_tasks = AsyncMock(return_value=0)
    bot._cancel_user_streaming = AsyncMock(return_value=False)
    bot._project_chat.stop = AsyncMock(return_value=False)
    update = _SerializableUpdate("/stop")

    await bot._cmd_stop(update, None)

    assert bot._followup_queue.depth(queue_key) == 0
    reply = update.message.reply_text.await_args.args[0]
    assert "Paused" in reply
    assert "Discarded 2 durably queued follow-ups" in reply


@pytest.mark.anyio
async def test_live_approval_reply_bypasses_followup_queue(tmp_path: Path) -> None:
    bot = _bot_harness(tmp_path / "queue.json")
    telegram_bot = SimpleNamespace(send_message=AsyncMock())
    bot.application = SimpleNamespace(bot=telegram_bot)
    bot._check_access = AsyncMock(return_value=True)
    bot._resolve_codex_approval_text = AsyncMock(return_value="allowed")
    update = Update.de_json(_update_payload("approve"), telegram_bot)

    await bot._handle_followup_text_update(update, None)

    telegram_bot.send_message.assert_awaited_once()
    assert "Approved" in telegram_bot.send_message.await_args.kwargs["text"]
    assert bot._followup_queue.conversation_keys() == ()
