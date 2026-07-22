"""Production scheduling contract for Codex distill snapshots (#465)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from telegram_bot.core.bot import TelegramBot
from telegram_bot.memory.codex_snapshot import CodexThreadSnapshotter
from telegram_bot.memory.distill_journal import DistillJournal
from telegram_bot.memory.distill_types import (
    CodexTranscriptSnapshot,
    DistillJobStatus,
    DistillTrigger,
    TranscriptBounds,
)


class RoutedSnapshotRuntime:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, str | None]] = []

    async def read_session_snapshot(
        self,
        session_id: str,
        *,
        bounds: TranscriptBounds,
        memory_audience: str | None,
        memory_scope: str | None,
    ) -> CodexTranscriptSnapshot:
        self.calls.append((session_id, memory_audience, memory_scope))
        return CodexTranscriptSnapshot(
            thread_hash=hashlib.sha256(session_id.encode()).hexdigest(),
            last_turn_id="turn-final",
            messages=(),
            byte_count=0,
            truncated=False,
            captured_at=datetime.now(timezone.utc).isoformat(),
        )


@pytest.mark.anyio
async def test_snapshotter_passes_only_the_journal_bound_route(tmp_path: Path) -> None:
    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    scope = "private-" + "a" * 32
    job = journal.enqueue_once(
        provider="codex",
        thread_id="thread-routed",
        trigger=DistillTrigger.NEW_COMMAND,
        memory_audience="private",
        memory_scope=scope,
    )
    runtime = RoutedSnapshotRuntime()
    worker = CodexThreadSnapshotter(journal, runtime, owner_token="snapshot-worker")

    result = await worker.snapshot_once(job_id=job.job_id)

    assert result.status is DistillJobStatus.SNAPSHOT_DONE
    assert runtime.calls == [("thread-routed", "private", scope)]


@pytest.mark.anyio
async def test_lifecycle_loop_recovers_queued_snapshot_job(tmp_path: Path) -> None:
    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    job = journal.enqueue_once(
        provider="codex",
        thread_id="thread-queued",
        trigger=DistillTrigger.AUTO_NEW,
        memory_audience="shared",
        memory_scope="shared",
    )
    runtime = RoutedSnapshotRuntime()
    worker = CodexThreadSnapshotter(journal, runtime, owner_token="loop-worker")
    bot = TelegramBot.__new__(TelegramBot)
    bot._distill_journal = journal
    bot._distill_snapshot_worker = worker
    bot._config = SimpleNamespace(distill_extraction_poll_interval=0.02)
    stop = asyncio.Event()
    task = asyncio.create_task(bot._distill_snapshot_loop(stop))
    try:
        deadline = asyncio.get_running_loop().time() + 2
        while (
            journal.get(job.job_id).status is not DistillJobStatus.SNAPSHOT_DONE
            and asyncio.get_running_loop().time() < deadline
        ):
            await asyncio.sleep(0.02)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=2)

    assert journal.get(job.job_id).status is DistillJobStatus.SNAPSHOT_DONE
    assert runtime.calls == [("thread-queued", "shared", "shared")]


@pytest.mark.anyio
async def test_lifecycle_loop_recovers_expired_snapshot_lease(tmp_path: Path) -> None:
    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    job = journal.enqueue_once(
        provider="codex",
        thread_id="thread-stale",
        trigger=DistillTrigger.NEW_COMMAND,
        memory_audience="shared",
        memory_scope="shared",
    )
    expired_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    claimed = journal.claim(
        job.job_id,
        owner_token="dead-worker",
        now=expired_at,
        lease_seconds=1,
    )
    assert claimed is not None
    assert claimed.status is DistillJobStatus.RUNNING_SNAPSHOT
    runtime = RoutedSnapshotRuntime()
    worker = CodexThreadSnapshotter(journal, runtime, owner_token="recovery-worker")
    bot = TelegramBot.__new__(TelegramBot)
    bot._distill_journal = journal
    bot._distill_snapshot_worker = worker
    bot._config = SimpleNamespace(distill_extraction_poll_interval=0.02)
    stop = asyncio.Event()
    task = asyncio.create_task(bot._distill_snapshot_loop(stop))
    try:
        deadline = asyncio.get_running_loop().time() + 2
        while (
            journal.get(job.job_id).status is not DistillJobStatus.SNAPSHOT_DONE
            and asyncio.get_running_loop().time() < deadline
        ):
            await asyncio.sleep(0.02)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=2)

    recovered = journal.get(job.job_id)
    assert recovered.status is DistillJobStatus.SNAPSHOT_DONE
    assert recovered.attempts == 2
