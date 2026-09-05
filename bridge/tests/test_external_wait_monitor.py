"""Deterministic coverage for the GitHub CI external-wait monitor (#740)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

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
async def test_terminal_notification_precedes_a_blocked_resume(tmp_path: Path) -> None:
    """A slow Codex admission must not hide a promptly detected CI result."""
    clock = Clock()
    registry = _registry(tmp_path, clock)
    recorder = Recorder()
    resume_started = asyncio.Event()
    release_resume = asyncio.Event()

    async def blocking_resume(record: dict, prompt: str) -> bool:
        recorder.resumes.append((record, prompt))
        resume_started.set()
        await release_resume.wait()
        return True

    monitor = _monitor(
        registry,
        FakeTransport([PrState("abc1234", "success")]),
        recorder,
        clock,
        resumer=blocking_resume,
    )

    tick = asyncio.create_task(monitor._tick())
    await asyncio.wait_for(resume_started.wait(), timeout=5)

    assert len(recorder.notifications) == 1
    assert "CI green" in recorder.notifications[0][1]
    assert "Continuing automatically." in recorder.notifications[0][1]
    assert not tick.done()

    release_resume.set()
    await tick
    assert registry.records()[0]["wake"]["resumed"] is True


@pytest.mark.anyio
async def test_resume_failure_sends_correction_and_records_dropped_promise(
    tmp_path: Path,
) -> None:
    clock = Clock()
    registry = _registry(tmp_path, clock)
    recorder = Recorder()

    async def failed_resume(record: dict, prompt: str) -> bool:
        recorder.resumes.append((record, prompt))
        return False

    monitor = _monitor(
        registry,
        FakeTransport([PrState("abc1234", "success")]),
        recorder,
        clock,
        resumer=failed_resume,
    )

    await monitor._tick()

    assert len(recorder.resumes) == 1
    assert len(recorder.notifications) == 2
    assert "Continuing automatically." in recorder.notifications[0][1]
    assert "NOT continued" in recorder.notifications[1][1]
    record = registry.records()[0]
    assert record["wake"]["state"] == "done"
    assert record["wake"]["resumed"] is False
    assert record["wake"]["skip_reason"] == "resume_failed"
    assert [item["wait_id"] for item in registry.dropped_promises()] == [record["wait_id"]]


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
async def test_legacy_short_sha_heals_instead_of_superseding(tmp_path: Path) -> None:
    # Regression (#961): a record written with a 7-char SHA watches the same
    # head whose headRefOid merely *extends* it — that is not a moved head.
    clock = Clock()
    registry = _registry(tmp_path, clock)  # seeds head_sha "abc1234" (7 chars)
    full = "abc1234" + "f" * 33
    transport = FakeTransport([PrState(full, "pending"), PrState(full, "success")])
    recorder = Recorder()
    monitor = _monitor(registry, transport, recorder, clock)

    await monitor._tick()

    record = registry.records()[0]
    assert record["state"] == "monitoring"
    assert record["head_sha"] == full

    clock.advance(120)
    await monitor._tick()

    record = registry.records()[0]
    assert record["terminal_status"] == TERMINAL_SUCCESS
    assert len(recorder.notifications) == 1


@pytest.mark.anyio
async def test_short_sha_prefix_of_a_different_head_still_supersedes(tmp_path: Path) -> None:
    # Same length, different content: not the watched head, so the wait ends.
    clock = Clock()
    registry = _registry(tmp_path, clock)
    transport = FakeTransport([PrState("abc9999" + "f" * 33, "success")])
    recorder = Recorder()
    monitor = _monitor(registry, transport, recorder, clock)

    await monitor._tick()

    record = registry.records()[0]
    assert record["terminal_status"] == TERMINAL_SUPERSEDED
    assert recorder.resumes == []


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
    # The skip must say so. The only previous signal was the absence of the
    # "Continuing automatically." line, which reads like a wake that continued.
    assert "NOT continued" in recorder.notifications[0][1]
    assert "another session" in recorder.notifications[0][1]
    # ...and it must be distinguishable in the ledger from a kept promise:
    # wake.state is "done" either way because the notification was delivered.
    record = registry.records()[0]
    assert record["wake"]["state"] == "done"
    assert record["wake"]["resumed"] is False
    assert record["wake"]["skip_reason"] == "session_moved"
    assert [rec["wait_id"] for rec in registry.dropped_promises()] == [record["wait_id"]]


@pytest.mark.anyio
async def test_resumed_promise_is_not_reported_as_dropped(tmp_path: Path) -> None:
    """The same-session path is the control for the test above."""
    clock = Clock()
    registry = _registry(tmp_path, clock)
    transport = FakeTransport([PrState("abc1234", "success")])
    recorder = Recorder()
    monitor = _monitor(registry, transport, recorder, clock)

    await monitor._tick()
    await monitor._tick()

    assert len(recorder.resumes) == 1
    record = registry.records()[0]
    assert record["wake"]["state"] == "done"
    assert record["wake"]["resumed"] is True
    assert "skip_reason" not in record["wake"]
    assert registry.dropped_promises() == []
    assert "Continuing automatically." in recorder.notifications[0][1]


@pytest.mark.anyio
async def test_skip_reasons_are_named_per_path(tmp_path: Path) -> None:
    clock = Clock()
    registry = _registry(tmp_path, clock)
    transport = FakeTransport([PrState("abc1234", "success")])
    recorder = Recorder()
    monitor = _monitor(registry, transport, recorder, clock, resume_daily_cap=0)

    await monitor._tick()
    await monitor._tick()

    record = registry.records()[0]
    assert record["wake"]["skip_reason"] == "daily_cap"
    assert [rec["wait_id"] for rec in registry.dropped_promises()] == [record["wait_id"]]


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
    assert recorder.resumes == []

    recorder.fail_notify = False
    await monitor._tick()
    assert len(recorder.notifications) == 1
    assert len(recorder.resumes) == 1
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


@pytest.mark.anyio
async def test_resume_turn_binds_the_canonical_conversation_session(tmp_path: Path) -> None:
    """Regression (2026-07-30): the resume turn ran detached from the conversation.

    bot.py's user-message path passes ``session_id`` from the session manager,
    but the external-wait resume closure omitted it. The continuation therefore
    ran under a fresh project-chat session, and ``publish_active_turn`` exported
    that detached id — so any wait the resumed turn registered was bound to a
    session the guard never sees, and its wake was skipped as "session moved
    on" while the ledger still marked the wake done. Chained CI waits were
    dropped structurally: PR #813 sat green and approved for ~3 hours.
    """
    calls: list[dict] = []

    class FakeProjectChat:
        async def process_message(self, prompt, user_id, chat_id, **kwargs):
            calls.append({"prompt": prompt, "user_id": user_id, **kwargs})
            return type("R", (), {"success": True, "content": "done"})()

    class FakeBot:
        async def send_message(self, chat_id: int, text: str, **kwargs) -> None:
            return None

    # The production bot composes lifecycle + delivery; the resume path sends
    # through the chunked delivery helper (#1109), so the test bot needs both.
    lifecycle = _lifecycle_for_delivery(tmp_path, FakeBot())
    lifecycle._project_chat = FakeProjectChat()  # type: ignore[attr-defined]
    lifecycle._session_manager = SimpleNamespace(  # type: ignore[attr-defined]
        get_session=lambda user_id: _session_of({"session_id": "canonical-session"})
    )

    monitor = lifecycle._build_external_wait_monitor()
    assert monitor is not None

    record = {
        "wait_id": "w1",
        "user_id": 7,
        "chat_id": 70,
        "session_id": "canonical-session",
        "repo": "jinwon-int/ccc-node",
        "pr_number": 813,
        "head_sha": "abc1234",
        "terminal_status": TERMINAL_SUCCESS,
        "summary": "squash-merge when green",
    }
    # Drive the live resume components directly (_deliver_wake's order); the
    # old _maybe_resume wrapper encoded the retired resume-before-notify order
    # and was removed as drifted dead code.
    assert await monitor._resume_skip_reason(record) is None
    assert await monitor._run_resume(record) is True
    assert len(calls) == 1
    # The whole point: the continuation is bound to the conversation, so a wait
    # registered inside it records an id the guard will still recognize.
    assert calls[0]["session_id"] == "canonical-session"


def test_skipped_notification_names_the_reason() -> None:
    record = {
        "terminal_status": TERMINAL_SUCCESS,
        "repo": "jinwon-int/ccc-node",
        "pr_number": 813,
        "head_sha": "abc1234def",
        "summary": "squash-merge when green",
    }
    text = wake_notification_text(record, resumed=False, skip_reason="session_moved")
    assert "NOT continued" in text and "another session" in text
    assert "Continuing automatically." not in text
    unknown = wake_notification_text(record, resumed=False, skip_reason="something-new")
    assert "NOT continued" in unknown


# ---------------------------------------------------------------------------
# Lifecycle-built callbacks: chunked delivery + failure journal (#1109)
# ---------------------------------------------------------------------------


def _lifecycle_for_delivery(tmp_path: Path, bot) -> object:
    """Lifecycle mixin wired like the composed bot, with a fake Telegram bot."""
    from telegram_bot.core import bot_delivery, bot_lifecycle

    class _LifecycleWithDelivery(
        bot_lifecycle.BotLifecycleMixin, bot_delivery.BotDeliveryMixin
    ):
        pass

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


@pytest.mark.anyio
async def test_lifecycle_notify_splits_long_text_into_chunks(tmp_path: Path) -> None:
    """A 5,000-char wake notification is delivered as 2+ chunks (#1109).

    The raw ``bot.send_message`` path dropped the whole notification past the
    Telegram size limit; the shared chunked path splits it instead.
    """
    sent: list[dict] = []

    class FakeBot:
        async def send_message(self, chat_id: int, text: str, **kwargs) -> None:
            sent.append({"chat_id": chat_id, "text": text})

    lifecycle = _lifecycle_for_delivery(tmp_path, FakeBot())
    monitor = lifecycle._build_external_wait_monitor()  # type: ignore[attr-defined]
    assert monitor is not None

    delivered = await monitor._notifier(70, "x" * 5000)

    assert delivered is True
    assert len(sent) >= 2
    assert all(len(chunk["text"]) <= 4096 for chunk in sent)
    assert sum(len(chunk["text"]) for chunk in sent) >= 5000


@pytest.mark.anyio
async def test_lifecycle_notify_retries_once_then_reports_failure(
    tmp_path: Path,
) -> None:
    import telegram.error

    attempts: list[int] = []

    class FailingBot:
        async def send_message(self, chat_id: int, text: str, **kwargs) -> None:
            attempts.append(chat_id)
            raise telegram.error.BadRequest("can't parse entities")

    lifecycle = _lifecycle_for_delivery(tmp_path, FailingBot())
    monitor = lifecycle._build_external_wait_monitor()  # type: ignore[attr-defined]
    assert monitor is not None

    delivered = await monitor._notifier(70, "wake up")

    assert delivered is False
    # One retry after the first failure: at least two send attempts.
    assert len(attempts) >= 2


@pytest.mark.anyio
async def test_lifecycle_resume_delivery_failure_is_journaled_on_the_record(
    tmp_path: Path,
) -> None:
    """Final resume-delivery failure stamps delivery_failed on the wait (#1109).

    Regression: 2026-08-14 10:29 KST seoseo journal — ``External-wait resume
    delivery failed: BadRequest`` left the owner with no message and no durable
    trace of why.
    """
    import telegram.error

    attempts: list[int] = []

    class FailingBot:
        async def send_message(self, chat_id: int, text: str, **kwargs) -> None:
            attempts.append(chat_id)
            raise telegram.error.BadRequest("message is too long")

    class FakeProjectChat:
        async def process_message(self, prompt, user_id, chat_id, **kwargs):
            return SimpleNamespace(success=True, content="turn output")

    lifecycle = _lifecycle_for_delivery(tmp_path, FailingBot())
    lifecycle._project_chat = FakeProjectChat()  # type: ignore[attr-defined]
    monitor = lifecycle._build_external_wait_monitor()  # type: ignore[attr-defined]
    assert monitor is not None

    # The lifecycle closure's registry resolves under bot_data_dir/external-wait.
    registry = ExternalWaitRegistry(default_registry_path(tmp_path / "external-wait"))
    wait_id = registry.register(
        repo="jinwon-int/ccc-node",
        pr_number=259,
        head_sha="abc1234",
        user_id=7,
        chat_id=70,
        session_id="sess-1",
        summary="watch CI",
        timeout_seconds=100_000,
        poll_interval_seconds=30,
    )

    record = {
        "wait_id": wait_id,
        "user_id": 7,
        "chat_id": 70,
        "session_id": "sess-1",
    }
    ok = await monitor._resumer(record, "resume prompt")

    assert ok is False
    assert len(attempts) >= 2  # retried once before giving up
    stored = registry._read()[wait_id]
    assert stored["delivery_failed"]["reason"] == "BadRequest"
    assert stored["delivery_failed"]["at"]


@pytest.mark.anyio
async def test_idle_monitor_skips_registry_reads_until_state_changes(
    tmp_path: Path,
) -> None:
    clock = Clock()
    registry = ExternalWaitRegistry(default_registry_path(tmp_path), clock=clock)
    reads = 0
    original_read = registry._read

    def counting_read():
        nonlocal reads
        reads += 1
        return original_read()

    registry._read = counting_read  # type: ignore[method-assign]
    transport = FakeTransport([PrState("abc1234", "pending")])
    recorder = Recorder()
    monitor = _monitor(registry, transport, recorder, clock)

    await monitor._tick()  # full pass proves the registry holds no work
    baseline = reads
    assert baseline > 0
    for _ in range(20):
        clock.advance(5)
        await monitor._tick()  # stat-only fast path: no reads, no parses
    assert reads == baseline

    # A new registration rewrites the file; the next tick must rescan and
    # actually poll the registered wait.
    registry.register(
        repo="jinwon-int/ccc-node",
        pr_number=1,
        head_sha="abc1234",
        user_id=7,
        chat_id=70,
        session_id="sess-1",
        summary="watch CI",
        timeout_seconds=1_000,
        poll_interval_seconds=5,
        now=clock(),
    )
    reads_before_tick = reads
    await monitor._tick()
    assert reads > reads_before_tick
    assert transport.calls
