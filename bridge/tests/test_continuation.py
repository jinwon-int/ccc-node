"""Unit tests for the durable yield-and-continue queue (#1113)."""

from __future__ import annotations

from pathlib import Path

import pytest

from telegram_bot.core.continuation import (
    MAX_CONSECUTIVE_FAILURES,
    STATE_CANCELLED,
    STATE_CAP_HOLD,
    STATE_PENDING,
    STATE_RUNNING,
    ContinuationQueue,
    ContinuationValidationError,
    default_queue_path,
    validate_prompt,
)


class Clock:
    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _queue(tmp_path: Path, clock: Clock) -> ContinuationQueue:
    return ContinuationQueue(default_queue_path(tmp_path), clock=clock)


def _register(queue: ContinuationQueue, **overrides) -> tuple[str, list[str]]:
    params = {
        "user_id": 7,
        "chat_id": 70,
        "session_id": "sess-1",
        "prompt": "open the content PR next",
    }
    params.update(overrides)
    return queue.register(**params)


def test_prompt_validation_is_bounded() -> None:
    with pytest.raises(ContinuationValidationError):
        validate_prompt("   ")
    with pytest.raises(ContinuationValidationError):
        validate_prompt("x" * 4001)
    assert validate_prompt("  many\n\nspaces   here ") == "many spaces here"


def test_register_replaces_pending_but_never_the_running_turn(
    tmp_path: Path,
) -> None:
    clock = Clock()
    queue = _queue(tmp_path, clock)
    first, _ = _register(queue)
    queue.mark_running(first)

    # A second registration inside the same turn replaces nothing running...
    second, replaced = _register(queue)
    assert replaced == []
    assert queue.get(first)["state"] == STATE_RUNNING
    assert queue.get(second)["state"] == STATE_PENDING

    # ...and a third replaces only the still-pending one.
    third, replaced = _register(queue)
    assert replaced == [second]
    assert queue.get(second)["state"] == STATE_CANCELLED
    assert queue.get(first)["state"] == STATE_RUNNING
    assert queue.get(third)["state"] == STATE_PENDING


def test_register_preserves_the_failure_counter_until_success_or_owner_action(
    tmp_path: Path,
) -> None:
    """A register-then-crash loop also registers — registration must not re-arm."""
    clock = Clock()
    queue = _queue(tmp_path, clock)
    cid, _ = _register(queue)
    queue.mark_running(cid)
    assert queue.mark_failed(cid, "boom") == 1
    cid2, _ = _register(queue)
    queue.mark_running(cid2)
    assert queue.mark_failed(cid2, "boom") == 2

    _register(queue)  # a new bundle alone does not reset the loop-guard
    assert queue.counter_for(7, 70)["consecutive_failures"] == 2

    queue.cancel_for(7, 70)  # /stop is the owner's reset
    assert queue.counter_for(7, 70)["consecutive_failures"] == 0


def test_mark_running_counts_daily_starts_and_rolls_over(tmp_path: Path) -> None:
    clock = Clock()
    queue = _queue(tmp_path, clock)
    cid, _ = _register(queue)
    assert queue.mark_running(cid) is True
    counter = queue.counter_for(7, 70)
    assert counter["count"] == 1
    day = counter["day"]

    queue.mark_done(cid)
    clock.advance(25 * 60 * 60)  # next UTC day
    cid2, _ = _register(queue)
    queue.mark_running(cid2)
    counter = queue.counter_for(7, 70)
    assert counter["day"] != day
    assert counter["count"] == 1


def test_done_resets_consecutive_failures(tmp_path: Path) -> None:
    clock = Clock()
    queue = _queue(tmp_path, clock)
    cid, _ = _register(queue)
    queue.mark_running(cid)
    queue.mark_failed(cid, "boom")
    cid2, _ = _register(queue)
    queue.mark_running(cid2)
    queue.mark_failed(cid2, "boom")
    assert queue.counter_for(7, 70)["consecutive_failures"] == 2

    cid3, _ = _register(queue)
    queue.mark_running(cid3)
    queue.mark_done(cid3)
    assert queue.counter_for(7, 70)["consecutive_failures"] == 0


def test_cap_hold_repend_rearms_pending_and_the_daily_counter(
    tmp_path: Path,
) -> None:
    clock = Clock()
    queue = _queue(tmp_path, clock)
    cid, _ = _register(queue)
    queue.mark_running(cid)
    queue.mark_done(cid)
    pending, _ = _register(queue)

    assert queue.mark_cap_hold(pending) is True
    assert queue.get(pending)["state"] == STATE_CAP_HOLD
    assert queue.pending() == []

    repended = queue.repend_cap_holds(7, 70)
    assert repended == [pending]
    assert queue.get(pending)["state"] == STATE_PENDING
    counter = queue.counter_for(7, 70)
    assert counter["count"] == 0
    assert counter["consecutive_failures"] == 0


def test_cap_hold_with_reason_names_it_without_touching_the_counter(
    tmp_path: Path,
) -> None:
    clock = Clock()
    queue = _queue(tmp_path, clock)
    cid, _ = _register(queue)
    queue.mark_running(cid)
    failures = queue.mark_failed(cid, "boom")
    assert failures == 1

    pending, _ = _register(queue)
    assert queue.mark_cap_hold(pending, reason="consecutive-failure-limit") is True
    rec = queue.get(pending)
    assert rec["state"] == STATE_CAP_HOLD
    assert rec["last_error"] == "consecutive-failure-limit"
    # The guard parks the chain; re-arming is /continue, not a side effect.
    assert queue.counter_for(7, 70)["consecutive_failures"] == 1


def test_cancel_for_clears_pending_and_running_and_resets_failures(
    tmp_path: Path,
) -> None:
    clock = Clock()
    queue = _queue(tmp_path, clock)
    running, _ = _register(queue)
    queue.mark_running(running)
    pending, _ = _register(queue)
    other, _ = _register(queue, user_id=9, chat_id=90)

    cancelled = queue.cancel_for(7, 70, include_running=True)

    assert set(cancelled) == {running, pending}
    assert queue.get(running)["state"] == STATE_CANCELLED
    assert queue.get(pending)["state"] == STATE_CANCELLED
    # Another conversation's queue is never touched.
    assert queue.get(other)["state"] == STATE_PENDING


def test_cancel_for_without_running_keeps_the_in_flight_turn(tmp_path: Path) -> None:
    clock = Clock()
    queue = _queue(tmp_path, clock)
    running, _ = _register(queue)
    queue.mark_running(running)

    cancelled = queue.cancel_for(7, 70)

    assert cancelled == []
    assert queue.get(running)["state"] == STATE_RUNNING


def test_terminal_records_prune_after_retention(tmp_path: Path) -> None:
    clock = Clock()
    queue = _queue(tmp_path, clock)
    cid, _ = _register(queue)
    queue.mark_running(cid)
    queue.mark_done(cid)
    clock.advance(25 * 60 * 60)

    _register(queue)  # triggers the prune on the next mutation
    assert queue.get(cid) is None


def test_max_consecutive_failures_constant_is_three() -> None:
    assert MAX_CONSECUTIVE_FAILURES == 3
