"""Continuation monitor: loop-guards, fail-closed start, lifecycle wiring (#1113)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from telegram_bot.core.continuation import (
    STATE_CAP_HOLD,
    STATE_DONE,
    STATE_FAILED,
    STATE_PENDING,
    ContinuationQueue,
    default_queue_path,
)
from telegram_bot.core.continuation_monitor import (
    ContinuationMonitor,
    cap_hold_notification_text,
    continuation_prompt_text,
    failure_stop_notification_text,
)
from telegram_bot.core.external_wait import default_active_turns_path


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Clock:
    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Recorder:
    def __init__(self, *, run_ok: bool = True) -> None:
        self.runs: list[tuple[dict, str]] = []
        self.notifications: list[tuple[int, str]] = []
        self.run_ok = run_ok

    async def run(self, record: dict, prompt: str) -> bool:
        self.runs.append((record, prompt))
        return self.run_ok

    async def notify(self, chat_id: int, text: str) -> bool:
        self.notifications.append((chat_id, text))
        return True


def _monitor(
    tmp_path: Path, recorder: Recorder, clock: Clock, **kwargs
) -> tuple[ContinuationMonitor, ContinuationQueue]:
    queue = ContinuationQueue(default_queue_path(tmp_path / "continuation"), clock=clock)
    options = {
        "runner": recorder.run,
        "notifier": recorder.notify,
        "active_turns_path": default_active_turns_path(tmp_path / "external-wait"),
        "clock": clock,
    }
    options.update(kwargs)
    return ContinuationMonitor(queue, **options), queue


def _register(queue: ContinuationQueue, **overrides) -> str:
    params = {
        "user_id": 7,
        "chat_id": 70,
        "session_id": "sess-1",
        "prompt": "open the content PR next",
    }
    params.update(overrides)
    cid, _ = queue.register(**params)
    return cid


def _publish_active_turn(tmp_path: Path, heartbeat: float) -> None:
    path = default_active_turns_path(tmp_path / "external-wait")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "7:70": {
                    "user_id": 7,
                    "chat_id": 70,
                    "session_id": "sess-1",
                    "heartbeat_epoch": heartbeat,
                }
            }
        )
    )


def test_prompt_text_is_external_event_origin() -> None:
    record = {"continuation_id": "abc123", "prompt": "do the next thing"}
    text = continuation_prompt_text(record)
    assert text.startswith("[external_event: continuation_queue id=abc123]")
    assert "do the next thing" in text


@pytest.mark.anyio
async def test_register_idle_tick_runs_the_bundle_and_marks_done(
    tmp_path: Path,
) -> None:
    """e2e: register -> turn ends -> monitor starts the bundle -> done."""
    clock = Clock()
    recorder = Recorder()
    monitor, queue = _monitor(tmp_path, recorder, clock)
    cid = _register(queue)

    await monitor._tick()

    assert len(recorder.runs) == 1
    record, prompt = recorder.runs[0]
    assert record["continuation_id"] == cid
    assert prompt.startswith("[external_event: continuation_queue")
    assert queue.get(cid)["state"] == STATE_DONE


@pytest.mark.anyio
async def test_active_turn_blocks_the_start(tmp_path: Path) -> None:
    """No continuation next to a live turn (double-start guard)."""
    clock = Clock()
    recorder = Recorder()
    monitor, queue = _monitor(tmp_path, recorder, clock)
    cid = _register(queue)
    _publish_active_turn(tmp_path, heartbeat=clock.now)

    await monitor._tick()

    assert recorder.runs == []
    assert queue.get(cid)["state"] == STATE_PENDING


@pytest.mark.anyio
async def test_unreadable_route_file_fails_closed(tmp_path: Path) -> None:
    """An undecidable route means 'active': never guess."""
    clock = Clock()
    recorder = Recorder()
    monitor, queue = _monitor(tmp_path, recorder, clock)
    cid = _register(queue)
    path = default_active_turns_path(tmp_path / "external-wait")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{corrupt")

    await monitor._tick()

    assert recorder.runs == []
    assert queue.get(cid)["state"] == STATE_PENDING


@pytest.mark.anyio
async def test_daily_tripwire_holds_and_notifies_then_continue_rearms(
    tmp_path: Path,
) -> None:
    clock = Clock()
    recorder = Recorder()
    monitor, queue = _monitor(tmp_path, recorder, clock, daily_cap=1)

    first = _register(queue)
    await monitor._tick()  # starts (count=1) and completes
    assert queue.get(first)["state"] == STATE_DONE

    second = _register(queue)
    await monitor._tick()  # tripwire: hold + notify, do not start
    assert queue.get(second)["state"] == STATE_CAP_HOLD
    assert len(recorder.runs) == 1
    assert len(recorder.notifications) == 1
    assert "/continue" in recorder.notifications[0][1]

    # The notification is once per hold transition, not per tick.
    await monitor._tick()
    assert len(recorder.notifications) == 1

    queue.repend_cap_holds(7, 70)  # what /continue does
    await monitor._tick()
    assert queue.get(second)["state"] == STATE_DONE
    assert len(recorder.runs) == 2


@pytest.mark.anyio
async def test_three_consecutive_failures_stop_the_chain_and_notify(
    tmp_path: Path,
) -> None:
    clock = Clock()
    recorder = Recorder(run_ok=False)
    monitor, queue = _monitor(tmp_path, recorder, clock)

    cids = []
    for _ in range(3):
        cids.append(_register(queue))
        await monitor._tick()  # starts, fails; the agent re-registers each time

    assert [queue.get(cid)["state"] for cid in cids] == [STATE_FAILED] * 3
    assert len(recorder.runs) == 3

    # The fourth registered bundle is NOT started: the guard parks the chain.
    fourth = _register(queue)
    await monitor._tick()
    assert queue.get(fourth)["state"] == STATE_CAP_HOLD
    assert queue.get(fourth)["last_error"] == "consecutive-failure-limit"
    assert len(recorder.runs) == 3
    assert any("/continue" in text for _, text in recorder.notifications)

    # The guard does not re-arm on registration alone.
    assert queue.counter_for(7, 70)["consecutive_failures"] == 3

    # Owner confirmation (/continue) re-arms: the parked bundle runs again.
    queue.repend_cap_holds(7, 70)
    await monitor._tick()
    assert len(recorder.runs) == 4
    assert queue.get(fourth)["state"] == STATE_FAILED


def test_notification_texts_name_the_escape() -> None:
    cap = cap_hold_notification_text({}, 20)
    assert "20" in cap and "/continue" in cap and "/stop" in cap
    stop = failure_stop_notification_text(3)
    assert "3" in stop and "/continue" in stop and "/stop" in stop


# ---------------------------------------------------------------------------
# Lifecycle-built monitor: flag contract + runner wiring (#1113)
# ---------------------------------------------------------------------------


def _lifecycle_for_continuation(tmp_path: Path, bot, monkeypatch) -> object:
    from telegram_bot.core import bot_delivery, bot_lifecycle

    class _LifecycleWithDelivery(
        bot_lifecycle.BotLifecycleMixin, bot_delivery.BotDeliveryMixin
    ):
        pass

    monkeypatch.setenv("CCC_CONTINUATION_ENABLED", "1")
    lifecycle = _LifecycleWithDelivery()
    lifecycle._config = SimpleNamespace(  # type: ignore[assignment]
        bot_data_dir=tmp_path, project_root=str(tmp_path)
    )
    lifecycle._session_manager = SimpleNamespace(  # type: ignore[assignment]
        get_session=lambda user_id: _session_of({"session_id": "sess-1"})
    )
    lifecycle._project_chat = SimpleNamespace()  # type: ignore[assignment]
    lifecycle.application = SimpleNamespace(bot=bot)
    return lifecycle


async def _session_of(session):
    return session


def test_continuation_monitor_is_none_when_the_flag_is_off(tmp_path: Path) -> None:
    from telegram_bot.core import bot_lifecycle

    lifecycle = bot_lifecycle.BotLifecycleMixin()
    lifecycle._config = SimpleNamespace(  # type: ignore[assignment]
        bot_data_dir=tmp_path, project_root=str(tmp_path)
    )
    # Default (unset) is off: no monitor, no queue writes, nothing happens.
    assert lifecycle._build_continuation_monitor() is None


@pytest.mark.anyio
async def test_lifecycle_runner_binds_session_runs_and_delivers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[dict] = []
    calls: list[dict] = []

    class FakeBot:
        async def send_message(self, chat_id: int, text: str, **kwargs) -> None:
            sent.append({"chat_id": chat_id, "text": text})

    class FakeProjectChat:
        async def process_message(self, prompt, user_id, chat_id, **kwargs):
            calls.append({"prompt": prompt, **kwargs})
            return SimpleNamespace(success=True, content="bundle done")

    lifecycle = _lifecycle_for_continuation(tmp_path, FakeBot(), monkeypatch)
    lifecycle._project_chat = FakeProjectChat()  # type: ignore[attr-defined]
    monitor = lifecycle._build_continuation_monitor()  # type: ignore[attr-defined]
    assert monitor is not None

    queue = ContinuationQueue(default_queue_path(tmp_path / "continuation"))
    cid, _ = queue.register(
        user_id=7, chat_id=70, session_id="sess-1", prompt="finish the rollout"
    )

    await monitor._tick()

    assert queue.get(cid)["state"] == STATE_DONE
    assert calls[0]["session_id"] == "sess-1"
    assert calls[0]["prompt"].startswith("[external_event: continuation_queue")
    assert any(chunk["text"] for chunk in sent)  # result delivered


@pytest.mark.anyio
async def test_lifecycle_runner_delivery_failure_marks_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import telegram.error

    class FailingBot:
        async def send_message(self, chat_id: int, text: str, **kwargs) -> None:
            raise telegram.error.BadRequest("nope")

    class FakeProjectChat:
        async def process_message(self, prompt, user_id, chat_id, **kwargs):
            return SimpleNamespace(success=True, content="bundle done")

    lifecycle = _lifecycle_for_continuation(tmp_path, FailingBot(), monkeypatch)
    lifecycle._project_chat = FakeProjectChat()  # type: ignore[attr-defined]
    monitor = lifecycle._build_continuation_monitor()  # type: ignore[attr-defined]
    assert monitor is not None

    queue = ContinuationQueue(default_queue_path(tmp_path / "continuation"))
    cid, _ = queue.register(
        user_id=7, chat_id=70, session_id="sess-1", prompt="finish the rollout"
    )

    await monitor._tick()

    assert queue.get(cid)["state"] == STATE_FAILED
    assert queue.counter_for(7, 70)["consecutive_failures"] == 1
