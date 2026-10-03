"""Merge-queue landing watch for durable external waits (#2118).

A queued PR whose speculative group run fails is dropped from the queue
silently: it stays OPEN with its approval, and neither its check rollup nor
its state changes, so the check-rollup source never wakes. These tests pin
the queue-membership watch that does.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from telegram_bot.core.external_wait import (
    SOURCE_GITHUB_MERGE_QUEUE,
    SOURCE_GITHUB_PR_CHECKS,
    TERMINAL_CLOSED,
    TERMINAL_EVICTED,
    TERMINAL_MERGED,
    TERMINAL_SUPERSEDED,
    ExternalWaitRegistry,
    ExternalWaitValidationError,
    _bounded_detail,
    default_registry_path,
    render_waits,
)
from telegram_bot.core.external_wait_monitor import (
    NOT_ENQUEUED_GRACE_SECONDS,
    ExternalWaitMonitor,
    MergeQueueState,
    TransportError,
    _failed_queue_run_id,
    _merge_queue_state_from,
    resume_prompt_text,
    wake_notification_text,
)
from telegram_bot.core.external_wait_status import render_wait_status

HEAD = "abc1234"


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


class QueueTransport:
    """Scripted merge-queue transport: each fetch pops one outcome (last repeats)."""

    def __init__(self, outcomes: list, failed_run: object = None) -> None:
        self.outcomes = list(outcomes)
        self.failed_run = failed_run
        self.queue_calls = 0
        self.run_lookups = 0
        self.pr_state_calls = 0

    async def fetch_pr_state(self, repo: str, pr_number: int):  # pragma: no cover - must not run
        self.pr_state_calls += 1
        raise AssertionError("merge-queue waits must not read the check rollup")

    async def fetch_merge_queue_state(self, repo: str, pr_number: int) -> MergeQueueState:
        self.queue_calls += 1
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def fetch_failed_queue_run(self, repo: str, pr_number: int):
        self.run_lookups += 1
        if isinstance(self.failed_run, Exception):
            raise self.failed_run
        return self.failed_run


class Recorder:
    def __init__(self) -> None:
        self.notifications: list[tuple[int, str]] = []
        self.resumes: list[tuple[dict, str]] = []

    async def notify(self, chat_id: int, text: str) -> bool:
        self.notifications.append((chat_id, text))
        return True

    async def resume(self, record: dict, prompt: str) -> bool:
        self.resumes.append((record, prompt))
        return True


async def _session_of(user_id: int, chat_id: int) -> str:
    return "sess-1"


def _queued(in_queue: bool = True, state: str = "OPEN", head: str = HEAD) -> MergeQueueState:
    return MergeQueueState(head_sha=head, pr_state=state, in_queue=in_queue)


def _setup(tmp_path: Path, transport: QueueTransport, clock: Clock):
    registry = ExternalWaitRegistry(default_registry_path(tmp_path), clock=clock)
    wait_id = registry.register(
        repo="jinwon-int/ccc-node",
        pr_number=2113,
        head_sha=HEAD,
        user_id=7,
        chat_id=70,
        session_id="sess-1",
        summary="clean up the branch",
        timeout_seconds=100_000,
        poll_interval_seconds=30,
        now=clock(),
        source=SOURCE_GITHUB_MERGE_QUEUE,
    )
    recorder = Recorder()
    monitor = ExternalWaitMonitor(
        registry,
        transport=transport,
        notifier=recorder.notify,
        resumer=recorder.resume,
        session_lookup=_session_of,
        clock=clock,
    )
    return registry, wait_id, recorder, monitor


async def _run(monitor: ExternalWaitMonitor, clock: Clock, ticks: int, step: float = 130) -> None:
    for _ in range(ticks):
        await monitor._tick()
        clock.advance(step)


@pytest.mark.anyio
async def test_queued_then_merged_wakes_once_and_resumes(tmp_path: Path) -> None:
    clock = Clock()
    transport = QueueTransport([_queued(), _queued(), _queued(in_queue=False, state="MERGED")])
    registry, wait_id, recorder, monitor = _setup(tmp_path, transport, clock)

    await _run(monitor, clock, 8)

    record = registry.get(wait_id)
    assert record["terminal_status"] == TERMINAL_MERGED
    assert record["queue_seen"] is True
    assert transport.pr_state_calls == 0
    assert len(recorder.notifications) == 1
    assert "Merged via merge queue" in recorder.notifications[0][1]
    assert len(recorder.resumes) == 1
    assert recorder.resumes[0][1].startswith(
        "[external_event: github_merge_queue terminal=merged repo=jinwon-int/ccc-node pr=2113"
    )


@pytest.mark.anyio
async def test_eviction_after_queue_seen_names_the_failed_group_run(tmp_path: Path) -> None:
    clock = Clock()
    transport = QueueTransport([_queued(), _queued(in_queue=False)], failed_run=36990888999)
    registry, wait_id, recorder, monitor = _setup(tmp_path, transport, clock)

    await _run(monitor, clock, 6)

    record = registry.get(wait_id)
    assert record["terminal_status"] == TERMINAL_EVICTED
    assert record["terminal_detail"] == {"reason": "dropped", "failed_run_id": 36990888999}
    assert transport.run_lookups == 1
    text = recorder.notifications[0][1]
    assert "Dropped from merge queue" in text and "36990888999" in text
    prompt = recorder.resumes[0][1]
    assert "terminal=evicted" in prompt and "failed_run=36990888999" in prompt


@pytest.mark.anyio
async def test_entry_still_queued_keeps_waiting(tmp_path: Path) -> None:
    clock = Clock()
    transport = QueueTransport([_queued()])
    registry, wait_id, recorder, monitor = _setup(tmp_path, transport, clock)

    await _run(monitor, clock, 10)

    assert registry.get(wait_id)["state"] == "monitoring"
    assert recorder.notifications == []
    # Queue polls back off but stay frequent enough for a minutes-long group run.
    assert registry.get(wait_id)["poll_interval_seconds"] <= 120


@pytest.mark.anyio
async def test_graphql_error_retries_without_going_terminal(tmp_path: Path) -> None:
    clock = Clock()
    transport = QueueTransport(
        [TransportError("rate-limit"), TransportError("gh-timeout"), _queued(), _queued(state="MERGED")]
    )
    registry, wait_id, recorder, monitor = _setup(tmp_path, transport, clock)

    await monitor._tick()
    clock.advance(130)
    assert registry.get(wait_id)["state"] == "monitoring"
    await _run(monitor, clock, 6)

    assert registry.get(wait_id)["terminal_status"] == TERMINAL_MERGED


@pytest.mark.anyio
async def test_never_enqueued_waits_out_the_grace_then_reports_it(tmp_path: Path) -> None:
    clock = Clock()
    transport = QueueTransport([_queued(in_queue=False)])
    registry, wait_id, recorder, monitor = _setup(tmp_path, transport, clock)

    await monitor._tick()
    assert registry.get(wait_id)["state"] == "monitoring"

    clock.advance(NOT_ENQUEUED_GRACE_SECONDS + 1)
    await _run(monitor, clock, 3)

    record = registry.get(wait_id)
    assert record["terminal_status"] == TERMINAL_EVICTED
    assert record["terminal_detail"] == {"reason": "never-enqueued"}
    assert transport.run_lookups == 0
    assert "never seen in the merge queue" in recorder.notifications[0][1]


@pytest.mark.anyio
async def test_late_enqueue_inside_the_grace_is_not_an_eviction(tmp_path: Path) -> None:
    clock = Clock()
    transport = QueueTransport([_queued(in_queue=False), _queued(), _queued(state="MERGED")])
    registry, wait_id, recorder, monitor = _setup(tmp_path, transport, clock)

    await _run(monitor, clock, 6, step=60)

    assert registry.get(wait_id)["terminal_status"] == TERMINAL_MERGED


@pytest.mark.anyio
async def test_closed_without_merge_notifies_but_does_not_resume(tmp_path: Path) -> None:
    clock = Clock()
    transport = QueueTransport([_queued(), _queued(in_queue=False, state="CLOSED")])
    registry, wait_id, recorder, monitor = _setup(tmp_path, transport, clock)

    await _run(monitor, clock, 5)

    assert registry.get(wait_id)["terminal_status"] == TERMINAL_CLOSED
    assert recorder.resumes == []
    assert "closed without merging" in recorder.notifications[0][1]
    assert "NOT continued" in recorder.notifications[0][1]


@pytest.mark.anyio
async def test_pushed_head_supersedes_the_queue_watch(tmp_path: Path) -> None:
    clock = Clock()
    transport = QueueTransport([_queued(), _queued(in_queue=False, head="def5678")])
    registry, wait_id, recorder, monitor = _setup(tmp_path, transport, clock)

    await _run(monitor, clock, 5)

    assert registry.get(wait_id)["terminal_status"] == TERMINAL_SUPERSEDED
    assert recorder.resumes == []


@pytest.mark.anyio
async def test_run_lookup_failure_still_reports_the_eviction(tmp_path: Path) -> None:
    clock = Clock()
    transport = QueueTransport(
        [_queued(), _queued(in_queue=False)], failed_run=TransportError("gh-error")
    )
    registry, wait_id, recorder, monitor = _setup(tmp_path, transport, clock)

    await _run(monitor, clock, 5)

    record = registry.get(wait_id)
    assert record["terminal_status"] == TERMINAL_EVICTED
    assert record["terminal_detail"] == {"reason": "dropped"}


def test_failed_queue_run_picks_the_newest_failed_run_for_this_pr() -> None:
    runs = [
        {"databaseId": 9, "headBranch": "gh-readonly-queue/main/pr-2114-aaa", "conclusion": "failure"},
        {"databaseId": 8, "headBranch": "gh-readonly-queue/main/pr-2113-bbb", "conclusion": "success"},
        {"databaseId": 7, "headBranch": "gh-readonly-queue/main/pr-2113-ccc", "conclusion": "failure"},
        {"databaseId": 6, "headBranch": "gh-readonly-queue/main/pr-2113-ddd", "conclusion": "failure"},
        {"databaseId": 5, "headBranch": "feature/pr-2113-x", "conclusion": "failure"},
    ]
    assert _failed_queue_run_id(runs, 2113) == 7
    assert _failed_queue_run_id(runs, 21) is None
    assert _failed_queue_run_id("not a list", 2113) is None


def test_merge_queue_state_parsing_is_fail_closed() -> None:
    state = _merge_queue_state_from(
        {"state": "OPEN", "headRefOid": "ABC1234", "isInMergeQueue": True}
    )
    assert state == MergeQueueState(head_sha="abc1234", pr_state="OPEN", in_queue=True)
    for bad in (None, {}, {"state": "DRAFT", "headRefOid": "abc"}, {"state": "OPEN"}):
        with pytest.raises(TransportError):
            _merge_queue_state_from(bad)


def test_registry_source_is_part_of_the_natural_key_and_validated(tmp_path: Path) -> None:
    registry = ExternalWaitRegistry(default_registry_path(tmp_path))
    common = dict(
        repo="jinwon-int/ccc-node",
        pr_number=5,
        head_sha=HEAD,
        user_id=7,
        chat_id=70,
        session_id="s",
        summary="x",
        timeout_seconds=600,
        poll_interval_seconds=30,
    )
    checks = registry.register(**common)
    queue = registry.register(**common, source=SOURCE_GITHUB_MERGE_QUEUE)
    assert checks != queue
    assert registry.register(**common, source=SOURCE_GITHUB_MERGE_QUEUE) == queue
    assert registry.get(checks)["source"] == SOURCE_GITHUB_PR_CHECKS
    with pytest.raises(ExternalWaitValidationError):
        registry.register(**common, source="webhook")
    assert "(merge queue)" in render_waits([registry.get(queue)])


def test_terminal_detail_is_bounded_and_identifier_shaped() -> None:
    assert _bounded_detail(
        {
            "failed_run_id": 123,
            "reason": "dropped",
            "log": "line one\nline two",
            "Bad-Key": 1,
            "flag": True,
            "huge": 10**20,
        }
    ) == {"failed_run_id": 123, "reason": "dropped"}


def test_status_message_names_the_merge_queue(tmp_path: Path) -> None:
    clock = Clock()
    registry = ExternalWaitRegistry(default_registry_path(tmp_path), clock=clock)
    wait_id = registry.register(
        repo="jinwon-int/ccc-node",
        pr_number=2113,
        head_sha=HEAD,
        user_id=7,
        chat_id=70,
        session_id="s",
        summary="",
        timeout_seconds=600,
        poll_interval_seconds=30,
        now=clock(),
        source=SOURCE_GITHUB_MERGE_QUEUE,
    )
    waiting = render_wait_status(registry.records(), now_epoch=clock())
    assert waiting is not None and "PR #2113 merge queue" in waiting
    registry.finish(wait_id, TERMINAL_EVICTED, now=clock(), detail={"reason": "dropped"})
    done = render_wait_status(registry.records(), now_epoch=clock() + 5)
    assert done is not None and "dropped from merge queue" in done


def test_check_rollup_prompt_is_unchanged() -> None:
    record = {
        "terminal_status": "success",
        "repo": "o/r",
        "pr_number": 1,
        "head_sha": "abcdef12",
        "summary": "merge",
    }
    assert resume_prompt_text(record) == (
        "[external_event: github_pr_checks terminal=success repo=o/r pr=1 head=abcdef12]\nmerge"
    )
    assert "Failed merge-group run" not in wake_notification_text(record, resumed=True)
