"""Regression coverage for #784 proposal 2 durable follow-up queuing."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import Update

from telegram_bot.core.bot import TelegramBot, _FollowupUpdateEnvelope
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
    bot._tasks = UserTaskQueue(3)
    bot.application = SimpleNamespace(bot=None)
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
