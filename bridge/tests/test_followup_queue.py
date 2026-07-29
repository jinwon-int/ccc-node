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
        followup_retry_backoff_seconds=(0.01, 0.02, 0.03),
        followup_worker_restart_cap=2,
        followup_worker_restart_backoff_seconds=0.01,
    )
    bot._project_chat = SimpleNamespace(
        busy_for_seconds=lambda user_id, chat_id, now: 120.0
    )
    bot._followup_queue = _queue(path, cap=cap)
    bot._followup_admission_locks = {}
    bot._followup_idle_events = {}
    bot._followup_live_counts = {}
    bot._followup_workers = {}
    bot._followup_worker_items = {}
    bot._followup_worker_disabled = set()
    bot._followup_worker_disable_notified = set()
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
async def test_failed_head_retries_have_real_wall_clock_backoff(
    tmp_path: Path,
) -> None:
    bot = _bot_harness(tmp_path / "queue.json")
    bot._config.followup_retry_backoff_seconds = (0.05, 0.1, 0.2)
    queue_key = bot._followup_queue_key("7:70")
    attempt_times: list[float] = []

    async def dispatch(_item) -> None:
        attempt_times.append(asyncio.get_running_loop().time())
        raise RuntimeError("transient failure")

    bot._dispatch_queued_followup = dispatch
    item, _ = bot._followup_queue.enqueue(
        conversation_key=queue_key,
        handler="text",
        update_json=json.dumps(_update_payload("retry me")),
        enqueued_at=1.0,
    )
    assert item is not None

    bot._start_followup_worker(queue_key)
    await _wait_until(lambda: bot._followup_queue.depth(queue_key) == 0)

    assert len(attempt_times) == bot._FOLLOWUP_MAX_ATTEMPTS
    gaps = [
        later - earlier
        for earlier, later in zip(attempt_times, attempt_times[1:])
    ]
    assert gaps[0] >= 0.045
    assert gaps[1] >= 0.09
    await bot._stop_followup_workers()


@pytest.mark.anyio
@pytest.mark.parametrize("write_site", ["acknowledge", "record_failure"])
async def test_queue_write_failure_pauses_worker_without_redispatch_storm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    write_site: str,
) -> None:
    bot = _bot_harness(tmp_path / f"{write_site}.json")
    queue_key = bot._followup_queue_key("7:70")
    dispatches = 0

    async def dispatch(_item) -> None:
        nonlocal dispatches
        dispatches += 1
        if write_site == "record_failure":
            raise RuntimeError("dispatch failed")

    bot._dispatch_queued_followup = dispatch
    disabled_notice = AsyncMock()
    bot._notify_followup_worker_disabled = disabled_notice
    monkeypatch.setattr(
        bot._followup_queue,
        write_site,
        Mock(side_effect=OSError(28, "No space left on device")),
    )
    item, _ = bot._followup_queue.enqueue(
        conversation_key=queue_key,
        handler="text",
        update_json=json.dumps(_update_payload("write failure")),
        enqueued_at=1.0,
    )
    assert item is not None

    bot._start_followup_worker(queue_key)
    await _wait_until(lambda: queue_key in bot._followup_worker_disabled)
    await _wait_until(lambda: queue_key not in bot._followup_workers)
    await asyncio.sleep(0.05)

    assert dispatches == 1
    disabled_notice.assert_awaited_once()
    assert bot._followup_queue.depth(queue_key) == 1
    await bot._stop_followup_workers()


@pytest.mark.anyio
async def test_discard_acknowledges_before_notification_and_never_storms(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = _bot_harness(tmp_path / "queue.json")
    queue_key = bot._followup_queue_key("7:70")
    item, _ = bot._followup_queue.enqueue(
        conversation_key=queue_key,
        handler="text",
        update_json=json.dumps(_update_payload("poison")),
        enqueued_at=1.0,
    )
    assert item is not None
    for _ in range(bot._FOLLOWUP_MAX_ATTEMPTS):
        item = bot._followup_queue.record_failure(item.item_id)
        assert item is not None

    failed_notice = AsyncMock(return_value=True)
    disabled_notice = AsyncMock()
    bot._notify_failed_followup = failed_notice
    bot._notify_followup_worker_disabled = disabled_notice
    monkeypatch.setattr(
        bot._followup_queue,
        "acknowledge",
        Mock(side_effect=OSError(30, "Read-only file system")),
    )

    bot._start_followup_worker(queue_key)
    await _wait_until(lambda: queue_key in bot._followup_worker_disabled)
    await _wait_until(lambda: queue_key not in bot._followup_workers)
    await asyncio.sleep(0.05)

    failed_notice.assert_not_awaited()
    disabled_notice.assert_awaited_once()
    assert bot._followup_queue.depth(queue_key) == 1
    assert bot._followup_queue.failure_notification_depth(queue_key) == 1
    await bot._stop_followup_workers()


@pytest.mark.anyio
async def test_unexpected_worker_failures_restart_with_backoff_then_disable(
    tmp_path: Path,
) -> None:
    bot = _bot_harness(tmp_path / "queue.json")
    bot._config.followup_worker_restart_cap = 2
    bot._config.followup_worker_restart_backoff_seconds = 0.03
    queue_key = bot._followup_queue_key("7:70")
    item, _ = bot._followup_queue.enqueue(
        conversation_key=queue_key,
        handler="text",
        update_json=json.dumps(_update_payload("retained")),
        enqueued_at=1.0,
    )
    assert item is not None
    starts: list[float] = []

    async def crash(_queue_key: str) -> None:
        starts.append(asyncio.get_running_loop().time())
        raise RuntimeError("unexpected worker crash")

    bot._run_followup_worker = crash
    disabled_notice = AsyncMock()
    bot._notify_followup_worker_disabled = disabled_notice

    bot._start_followup_worker(queue_key)
    await _wait_until(lambda: queue_key in bot._followup_worker_disabled)
    await _wait_until(lambda: queue_key not in bot._followup_workers)

    assert len(starts) == 3
    assert starts[1] - starts[0] >= 0.025
    assert starts[2] - starts[1] >= 0.055
    disabled_notice.assert_awaited_once()
    assert bot._followup_queue.depth(queue_key) == 1
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
    worker = bot._followup_workers[queue_key]
    bot._start_followup_worker(queue_key)
    bot._start_followup_worker(queue_key)
    assert bot._followup_workers[queue_key] is worker
    assert sum(
        task.get_name() == f"followup-queue:{queue_key}"
        and not task.done()
        for task in asyncio.all_tasks()
    ) == 1

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

    update = _SerializableUpdate("legacy fallback")
    run_task = AsyncMock()
    on_overflow = AsyncMock()
    accepted = False

    async def enqueue_followup(_update, _context) -> None:
        nonlocal accepted
        accepted = await bot._enqueue_user_task(
            "7:70",
            run_task,
            on_overflow,
        )

    await bot._with_followup_update(
        handler="text",
        update=update,
        context=None,
        callback=enqueue_followup,
    )
    await _wait_until(lambda: run_task.await_count == 1)

    assert accepted is True
    on_overflow.assert_not_awaited()
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
async def test_failed_discard_notification_is_durable_and_surfaces_later(
    tmp_path: Path,
) -> None:
    path = tmp_path / "queue.json"
    bot = _bot_harness(path)
    queue_key = bot._followup_queue_key("7:70")

    async def dispatch(_item) -> None:
        raise RuntimeError("dispatch outage")

    bot._dispatch_queued_followup = dispatch
    bot.application.bot.send_message = AsyncMock(
        side_effect=RuntimeError("Telegram outage")
    )
    item, _ = bot._followup_queue.enqueue(
        conversation_key=queue_key,
        handler="text",
        update_json=json.dumps(_update_payload("tell me the outcome")),
        enqueued_at=1.0,
    )
    assert item is not None

    bot._start_followup_worker(queue_key)
    await _wait_until(lambda: bot._followup_queue.depth(queue_key) == 0)
    await _wait_until(lambda: queue_key not in bot._followup_workers)

    assert bot.application.bot.send_message.await_count == 4
    restarted_queue = _queue(path)
    assert restarted_queue.failure_notification_depth(queue_key) == 1

    delivered = AsyncMock()
    bot._followup_queue = restarted_queue
    bot.application.bot.send_message = delivered
    bot._start_followup_worker(queue_key)
    await _wait_until(
        lambda: bot._followup_queue.failure_notification_depth(queue_key) == 0
    )

    delivered.assert_awaited_once()
    assert "could not be processed after 3 attempts" in (
        delivered.await_args.kwargs["text"]
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
    other_queue_key = bot._followup_queue_key("8:80")
    for index in (1, 2):
        item, _ = bot._followup_queue.enqueue(
            conversation_key=queue_key,
            handler="text",
            update_json=json.dumps(_update_payload(str(index), update_id=index)),
            enqueued_at=float(index),
        )
        assert item is not None
    other_item, _ = bot._followup_queue.enqueue(
        conversation_key=other_queue_key,
        handler="text",
        update_json=json.dumps(_update_payload("other", update_id=3)),
        enqueued_at=3.0,
    )
    assert other_item is not None
    bot._check_access = AsyncMock(return_value=True)
    bot._deny_codex_approvals = Mock()
    bot._invalidate_codex_approvals = Mock()
    bot._cancel_user_voice_tasks = AsyncMock(return_value=0)
    bot._cancel_user_streaming = AsyncMock(return_value=False)
    bot._project_chat.stop = AsyncMock(return_value=False)
    update = _SerializableUpdate("/stop")

    await bot._cmd_stop(update, None)

    assert bot._followup_queue.depth(queue_key) == 0
    assert bot._followup_queue.depth(other_queue_key) == 1
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


def _stage_across_two_conversations(queue: PersistentFollowupQueue) -> tuple[int, int]:
    """Enqueue B before A, then discard A first so notices invert id order."""

    key_b = '["str","8:80"]'
    key_a = '["str","7:70"]'
    older, _ = queue.enqueue(
        conversation_key=key_b,
        handler="text",
        update_json=json.dumps(_update_payload("b-first", update_id=1)),
        enqueued_at=1.0,
    )
    newer, _ = queue.enqueue(
        conversation_key=key_a,
        handler="text",
        update_json=json.dumps(_update_payload("a-second", update_id=2)),
        enqueued_at=2.0,
    )
    assert older is not None and newer is not None
    assert older.item_id < newer.item_id
    # Chat A discards first (newer id), then chat B (older id).
    assert queue.stage_failure_notification(newer.item_id) is not None
    assert queue.acknowledge(newer.item_id) is True
    assert queue.stage_failure_notification(older.item_id) is not None
    assert queue.acknowledge(older.item_id) is True
    return older.item_id, newer.item_id


def test_cross_conversation_discards_do_not_corrupt_the_shared_queue(
    tmp_path: Path,
) -> None:
    """Notices staged out of enqueue order must still pass _read_data.

    Regression: notices were appended in discard order while the reader
    required each collection to be strictly id-ascending, so two chats
    discarding out of order wrote a file that failed our own validator and
    took down message processing for EVERY conversation until an operator
    deleted the file by hand.
    """

    path = tmp_path / "followup-queue.json"
    queue = _queue(path)
    older_id, newer_id = _stage_across_two_conversations(queue)

    # Every read path must still work on the freshly written file.
    assert queue.peek('["str","7:70"]') is None
    assert queue.depth('["str","8:80"]') == 0
    assert set(queue.conversation_keys()) == {'["str","7:70"]', '["str","8:80"]'}
    assert queue.failure_notification_depth('["str","7:70"]') == 1
    assert queue.failure_notification_depth('["str","8:80"]') == 1

    # And a cold start on that same file must not fail closed.
    reopened = PersistentFollowupQueue(path, per_chat_cap=32)
    reopened.initialize()
    assert reopened.peek_failure_notification('["str","7:70"]') is not None
    assert reopened.peek_failure_notification('["str","8:80"]') is not None

    stored = json.loads(path.read_text(encoding="utf-8"))
    ids = [item["id"] for item in stored["failure_notifications"]]
    assert ids == sorted(ids) == [older_id, newer_id]
