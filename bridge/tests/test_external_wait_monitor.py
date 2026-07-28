"""Deterministic coverage for the GitHub CI external-wait monitor (#740)."""

from __future__ import annotations

from pathlib import Path

import pytest

from telegram_bot.core.external_wait import (
    TERMINAL_CANCELLED,
    TERMINAL_EXPIRED,
    TERMINAL_FAILURE,
    TERMINAL_MONITOR_ERROR,
    TERMINAL_SUPERSEDED,
    TERMINAL_SUCCESS,
    ExternalWaitRegistry,
    default_registry_path,
)
from telegram_bot.core.external_wait_monitor import (
    ExternalWaitMonitor,
    PrState,
    TransportError,
    _normalize_rollup,
    resume_prompt_text,
    wake_notification_text,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Clock:
    def __init__(self, start: float = 1_100.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeTransport:
    """Scripted transport: each fetch pops one outcome (or repeats the last)."""

    def __init__(self, outcomes: list) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[str, int]] = []

    async def fetch_pr_state(self, repo: str, pr_number: int) -> PrState:
        self.calls.append((repo, pr_number))
        if len(self.outcomes) > 1:
            outcome = self.outcomes.pop(0)
        else:
            outcome = self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class Recorder:
    def __init__(self, *, fail_notify: bool = False) -> None:
        self.notifications: list[tuple[int, str]] = []
        self.resumes: list[tuple[dict, str]] = []
        self.fail_notify = fail_notify

    async def notify(self, chat_id: int, text: str) -> bool:
        if self.fail_notify:
            raise RuntimeError("telegram down")
        self.notifications.append((chat_id, text))
        return True

    async def resume(self, record: dict, prompt: str) -> bool:
        self.resumes.append((record, prompt))
        return True


async def _session_of(session_id):
    return session_id


def _registry(tmp_path: Path, clock: Clock, **wait) -> ExternalWaitRegistry:
    registry = ExternalWaitRegistry(default_registry_path(tmp_path), clock=clock)
    params = {
        "repo": "jinwon-int/ccc-node",
        "pr_number": 123,
        "head_sha": "abc1234",
        "user_id": 7,
        "chat_id": 70,
        "session_id": "sess-1",
        "summary": "squash-merge the PR",
        "timeout_seconds": 100_000,
        "poll_interval_seconds": 30,
        "now": 1_000.0,
    }
    params.update(wait)
    registry.register(**params)
    return registry


def _monitor(registry, transport, recorder, clock, **kwargs) -> ExternalWaitMonitor:
    options = {
        "transport": transport,
        "notifier": recorder.notify,
        "resumer": recorder.resume,
        "session_lookup": lambda user_id, chat_id: _session_of("sess-1"),
        "clock": clock,
    }
    options.update(kwargs)
    return ExternalWaitMonitor(registry, **options)


def test_rollup_normalization_covers_terminal_and_pending_states() -> None:
    assert _normalize_rollup({"statusCheckRollup": []}) == "pending"
    assert _normalize_rollup(
        {"statusCheckRollup": [{"conclusion": "SUCCESS"}, {"conclusion": "NEUTRAL"}]}
    ) == "success"
    assert _normalize_rollup(
        {"statusCheckRollup": [{"conclusion": "SUCCESS"}, {"conclusion": "FAILURE"}]}
    ) == "failure"
    assert _normalize_rollup(
        {"statusCheckRollup": [{"conclusion": "CANCELLED"}]}
    ) == "cancelled"
    assert _normalize_rollup(
        {"statusCheckRollup": [{"status": "IN_PROGRESS"}]}
    ) == "pending"


@pytest.mark.anyio
async def test_pending_to_success_wakes_once_and_resumes(tmp_path: Path) -> None:
    clock = Clock()
    registry = _registry(tmp_path, clock)
    transport = FakeTransport(
        [PrState("abc1234", "pending"), PrState("abc1234", "success")]
        + [PrState("abc1234", "success")] * 8
    )
    recorder = Recorder()
    monitor = _monitor(registry, transport, recorder, clock)

    for _ in range(10):
        await monitor._tick()
        clock.advance(120)

    assert len(recorder.notifications) == 1
    chat_id, text = recorder.notifications[0]
    assert chat_id == 70
    assert "CI green" in text and "jinwon-int/ccc-node#123" in text
    assert "Continuing automatically." in text
    # The continuation is exactly-once and clearly bridge-owned (#740).
    assert len(recorder.resumes) == 1
    record, prompt = recorder.resumes[0]
    assert record["terminal_status"] == TERMINAL_SUCCESS
    assert prompt.startswith("[external_event: github_pr_checks terminal=success")
    assert "squash-merge the PR" in prompt


@pytest.mark.anyio
async def test_failure_and_cancelled_are_terminal_results(tmp_path: Path) -> None:
    for rollup, terminal, headline in (
        ("failure", TERMINAL_FAILURE, "CI failed"),
        ("cancelled", TERMINAL_CANCELLED, "CI cancelled"),
    ):
        clock = Clock()
        registry = _registry(tmp_path / rollup, clock)
        transport = FakeTransport([PrState("abc1234", rollup)])
        recorder = Recorder()
        monitor = _monitor(registry, transport, recorder, clock)

        await monitor._tick()
        await monitor._tick()

        record = registry.records()[0]
        assert record["terminal_status"] == terminal
        assert len(recorder.notifications) == 1
        assert headline in recorder.notifications[0][1]


@pytest.mark.anyio
async def test_head_sha_mismatch_is_superseded_never_success(tmp_path: Path) -> None:
    clock = Clock()
    registry = _registry(tmp_path, clock)
    transport = FakeTransport([PrState("fff9999", "success")])
    recorder = Recorder()
    monitor = _monitor(registry, transport, recorder, clock)

    await monitor._tick()
    await monitor._tick()

    record = registry.records()[0]
    assert record["terminal_status"] == TERMINAL_SUPERSEDED
    assert len(recorder.notifications) == 1
    assert "head moved" in recorder.notifications[0][1]
    # A stale run says nothing about the watched CI: never resume from it.
    assert recorder.resumes == []


@pytest.mark.anyio
async def test_restart_recovery_repolls_and_drains_pending_wake(tmp_path: Path) -> None:
    clock = Clock()
    registry = _registry(tmp_path, clock)
    first_monitor = _monitor(
        registry, FakeTransport([PrState("abc1234", "success")]), Recorder(), clock
    )
    await first_monitor._tick()  # terminal journaled + wake delivered

    # Simulate a restart after the journal write but before the drain: force
    # the wake back to pending (as if the process died mid-delivery).
    wait_id = registry.records()[0]["wait_id"]
    registry.mark_wake(wait_id, delivered=False)
    assert registry.pending_wakes()

    recorder = Recorder()
    recovered = _monitor(registry, FakeTransport([]), recorder, clock)
    await recovered._tick()  # drains without re-polling GitHub

    assert len(recorder.notifications) == 1
    assert registry.pending_wakes() == []


@pytest.mark.anyio
async def test_transport_errors_fail_closed_after_bounded_retries(tmp_path: Path) -> None:
    clock = Clock()
    registry = _registry(tmp_path, clock)
    transport = FakeTransport([TransportError("rate-limit")])
    recorder = Recorder()
    monitor = _monitor(registry, transport, recorder, clock)

    for _ in range(3):
        await monitor._tick()
        clock.advance(400)
    await monitor._tick()  # drain the monitor-error wake

    record = registry.records()[0]
    assert record["terminal_status"] == TERMINAL_MONITOR_ERROR
    assert len(recorder.notifications) == 1
    assert "read error" in recorder.notifications[0][1]


@pytest.mark.anyio
async def test_expired_wait_notifies_without_resume(tmp_path: Path) -> None:
    clock = Clock(9_999.0)
    registry = _registry(tmp_path, clock, timeout_seconds=600)
    transport = FakeTransport([PrState("abc1234", "pending")])
    recorder = Recorder()
    monitor = _monitor(registry, transport, recorder, clock)

    await monitor._tick()
    await monitor._tick()

    record = registry.records()[0]
    assert record["terminal_status"] == TERMINAL_EXPIRED
    assert len(recorder.notifications) == 1
    assert "expired" in recorder.notifications[0][1]
    assert recorder.resumes == []


@pytest.mark.anyio
async def test_resume_is_skipped_when_session_moved_on(tmp_path: Path) -> None:
    clock = Clock()
    registry = _registry(tmp_path, clock)
    transport = FakeTransport([PrState("abc1234", "success")])
    recorder = Recorder()
    monitor = _monitor(
        registry,
        transport,
        recorder,
        clock,
        session_lookup=lambda user_id, chat_id: _session_of("different-session"),
    )

    await monitor._tick()
    await monitor._tick()

    assert recorder.resumes == []
    assert len(recorder.notifications) == 1
    assert "Continuing automatically." not in recorder.notifications[0][1]


@pytest.mark.anyio
async def test_resume_daily_cap_falls_back_to_notification(tmp_path: Path) -> None:
    clock = Clock()
    registry = _registry(tmp_path, clock)
    transport = FakeTransport([PrState("abc1234", "success")])
    recorder = Recorder()
    monitor = _monitor(registry, transport, recorder, clock, resume_daily_cap=0)

    await monitor._tick()
    await monitor._tick()

    assert recorder.resumes == []
    assert len(recorder.notifications) == 1


@pytest.mark.anyio
async def test_resume_is_skipped_without_a_promised_next_step(tmp_path: Path) -> None:
    clock = Clock()
    registry = _registry(tmp_path, clock, summary="")
    transport = FakeTransport([PrState("abc1234", "success")])
    recorder = Recorder()
    monitor = _monitor(registry, transport, recorder, clock)

    await monitor._tick()
    await monitor._tick()

    assert recorder.resumes == []
    assert len(recorder.notifications) == 1


@pytest.mark.anyio
async def test_notifier_failure_retries_until_delivered(tmp_path: Path) -> None:
    clock = Clock()
    registry = _registry(tmp_path, clock)
    recorder = Recorder(fail_notify=True)
    monitor = _monitor(registry, FakeTransport([PrState("abc1234", "success")]), recorder, clock)

    await monitor._tick()  # terminal journaled; wake attempted, notifier raises
    await monitor._tick()
    assert registry.pending_wakes()  # still pending, will retry

    recorder.fail_notify = False
    await monitor._tick()
    assert len(recorder.notifications) == 1
    assert registry.pending_wakes() == []


def test_notification_and_prompt_texts_are_body_free() -> None:
    record = {
        "terminal_status": TERMINAL_FAILURE,
        "repo": "jinwon-int/ccc-node",
        "pr_number": 123,
        "head_sha": "abc1234def",
        "summary": "inspect the failing checks",
    }
    text = wake_notification_text(record, resumed=False)
    assert "CI failed" in text and "abc1234d" in text
    prompt = resume_prompt_text(record)
    assert prompt.startswith("[external_event: github_pr_checks terminal=failure")
