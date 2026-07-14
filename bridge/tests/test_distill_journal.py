"""RED-first contract tests for the Codex distill snapshot journal (#473)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import os
from pathlib import Path
import stat
from unittest.mock import patch

import pytest

from telegram_bot.memory.codex_snapshot import CodexThreadSnapshotter
from telegram_bot.memory.distill_journal import DistillJournal
from telegram_bot.memory.distill_types import (
    CodexTranscriptSnapshot,
    DistillJobStatus,
    DistillTrigger,
    TranscriptBounds,
    TranscriptMessage,
)


def utc(second: int = 0) -> datetime:
    return datetime(2026, 7, 14, 6, 0, second, tzinfo=timezone.utc)


def snapshot() -> CodexTranscriptSnapshot:
    return CodexTranscriptSnapshot(
        thread_hash="a" * 64,
        last_turn_id="turn-1",
        messages=(TranscriptMessage("user", "hello", "2026-07-14T06:00:00Z"),),
        byte_count=5,
        truncated=False,
        captured_at="2026-07-14T06:00:00Z",
    )


def test_enqueue_once_is_concurrency_safe_idempotent_and_private(tmp_path: Path) -> None:
    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()

    def enqueue():
        return journal.enqueue_once(
            provider="codex",
            thread_id="thread-sensitive-value",
            trigger=DistillTrigger.NEW_COMMAND,
        )

    with ThreadPoolExecutor(max_workers=10) as pool:
        jobs = list(pool.map(lambda _: enqueue(), range(10)))

    assert len({job.job_id for job in jobs}) == 1
    assert len(journal.list_jobs()) == 1
    assert jobs[0].status is DistillJobStatus.QUEUED
    assert stat.S_IMODE(journal.root.stat().st_mode) == 0o700
    assert stat.S_IMODE(journal.job_path(jobs[0].job_id).stat().st_mode) == 0o600


def test_job_key_is_cross_trigger_idempotent_and_binds_discriminator_and_schema(
    tmp_path: Path,
) -> None:
    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    first = journal.enqueue_once(
        provider="codex",
        thread_id="thread-1",
        trigger=DistillTrigger.NEW_COMMAND,
    )
    duplicate = journal.enqueue_once(
        provider="codex",
        thread_id="thread-1",
        trigger=DistillTrigger.AUTO_NEW,
    )
    different_transcript = journal.enqueue_once(
        provider="codex",
        thread_id="thread-1",
        trigger=DistillTrigger.AUTO_NEW,
        discriminator="last-turn-2",
    )
    different_schema = journal.enqueue_once(
        provider="codex",
        thread_id="thread-1",
        trigger=DistillTrigger.AUTO_NEW,
        schema_version=2,
    )

    assert first.job_id == duplicate.job_id
    assert different_transcript.job_id != first.job_id
    assert different_schema.job_id != first.job_id
    assert len(journal.list_jobs()) == 3


def test_journal_rejects_symlink_hardlink_foreign_owner_and_unsafe_mode(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    linked = tmp_path / "linked"
    linked.symlink_to(target, target_is_directory=True)
    with pytest.raises(PermissionError):
        DistillJournal(linked).initialize()

    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o700)
    unsafe.chmod(0o750)
    with pytest.raises(PermissionError):
        DistillJournal(unsafe).initialize()

    unsafe_lock = DistillJournal(tmp_path / "unsafe-lock")
    unsafe_lock.initialize()
    (unsafe_lock.root / ".journal.lock").chmod(0o640)
    with pytest.raises(PermissionError):
        DistillJournal(unsafe_lock.root).initialize()

    journal = DistillJournal(tmp_path / "safe")
    journal.initialize()
    job = journal.enqueue_once(
        provider="codex",
        thread_id="thread-1",
        trigger=DistillTrigger.AUTO_NEW,
    )
    os.link(journal.job_path(job.job_id), tmp_path / "second-link.json")
    with pytest.raises(PermissionError):
        journal.get(job.job_id)

    foreign = DistillJournal(tmp_path / "foreign")
    foreign.initialize()
    original_uid = os.getuid()
    with patch(
        "telegram_bot.memory.distill_journal.os.getuid",
        return_value=original_uid + 1,
    ), pytest.raises(PermissionError):
        foreign.validate_path()


def test_claim_transitions_are_fenced_and_stale_running_recovers(tmp_path: Path) -> None:
    root = tmp_path / "journal"
    journal = DistillJournal(root)
    journal.initialize()
    queued = journal.enqueue_once(
        provider="codex",
        thread_id="thread-1",
        trigger=DistillTrigger.PROVIDER_SWITCH,
    )

    running = journal.claim(
        queued.job_id,
        owner_token="worker-a",
        now=utc(),
        lease_seconds=10,
    )
    assert running.status is DistillJobStatus.RUNNING_SNAPSHOT
    assert running.attempts == 1
    assert running.lease_epoch == 1
    assert journal.claim(
        queued.job_id,
        owner_token="worker-b",
        now=utc(1),
        lease_seconds=10,
    ) is None

    journal = DistillJournal(root)
    journal.initialize()
    assert journal.get(queued.job_id).status is DistillJobStatus.RUNNING_SNAPSHOT
    assert journal.recover_stale_running(now=utc(5)) == 0
    assert journal.recover_stale_running(now=utc(11)) == 1

    rerun = journal.claim(
        queued.job_id,
        owner_token="worker-b",
        now=utc(12),
        lease_seconds=10,
    )
    assert rerun.status is DistillJobStatus.RUNNING_SNAPSHOT
    assert rerun.lease_epoch == 2
    with pytest.raises(RuntimeError, match="owner or lease"):
        journal.mark_snapshot_done(
            queued.job_id,
            owner_token="worker-a",
            lease_epoch=1,
            snapshot=snapshot(),
            now=utc(13),
        )

    done = journal.mark_snapshot_done(
        queued.job_id,
        owner_token="worker-b",
        lease_epoch=2,
        snapshot=snapshot(),
        now=utc(13),
    )
    assert done.status is DistillJobStatus.SNAPSHOT_DONE
    with pytest.raises(RuntimeError, match="transition"):
        journal.mark_retryable_failed(
            queued.job_id,
            owner_token="worker-b",
            lease_epoch=2,
            error_code="late",
            now=utc(14),
        )


@pytest.mark.anyio
async def test_queued_job_survives_reopen_and_snapshotter_uses_read_only_runtime(
    tmp_path: Path,
) -> None:
    root = tmp_path / "journal"
    first = DistillJournal(root)
    first.initialize()
    queued = first.enqueue_once(
        provider="codex",
        thread_id="thread-old",
        trigger=DistillTrigger.NEW_COMMAND,
    )

    reopened = DistillJournal(root)
    reopened.initialize()

    class Runtime:
        def __init__(self) -> None:
            self.calls = []

        async def read_session_snapshot(self, session_id, *, bounds):
            self.calls.append((session_id, bounds))
            return snapshot()

    runtime = Runtime()
    worker = CodexThreadSnapshotter(
        reopened,
        runtime,
        bounds=TranscriptBounds(max_bytes=128),
        owner_token="snapshot-worker",
    )

    result = await worker.snapshot_once(job_id=queued.job_id)
    assert result.status is DistillJobStatus.SNAPSHOT_DONE
    assert runtime.calls == [("thread-old", TranscriptBounds(max_bytes=128))]


@pytest.mark.anyio
async def test_snapshotter_records_fixed_retryable_code_without_raw_exception(
    tmp_path: Path,
) -> None:
    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    queued = journal.enqueue_once(
        provider="codex",
        thread_id="thread-failure",
        trigger=DistillTrigger.AUTO_NEW,
    )

    class Runtime:
        async def read_session_snapshot(self, session_id, *, bounds):
            del session_id, bounds
            raise RuntimeError("credential=raw-secret")

    result = await CodexThreadSnapshotter(
        journal,
        Runtime(),
        owner_token="snapshot-worker",
    ).snapshot_once(job_id=queued.job_id)

    assert result.status is DistillJobStatus.RETRYABLE_FAILED
    assert result.error_code == "snapshot_read_failed"
    diagnostics = repr(journal.diagnostics(queued.job_id))
    assert "raw-secret" not in diagnostics
    assert "credential" not in diagnostics


def test_diagnostics_never_expose_thread_or_transcript_body(tmp_path: Path) -> None:
    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    job = journal.enqueue_once(
        provider="codex",
        thread_id="raw-thread-id-must-not-leak",
        trigger=DistillTrigger.NEW_COMMAND,
    )
    running = journal.claim(
        job.job_id,
        owner_token="worker-secret-token",
        now=utc(),
        lease_seconds=10,
    )
    assert running is not None
    journal.mark_snapshot_done(
        job.job_id,
        owner_token="worker-secret-token",
        lease_epoch=running.lease_epoch,
        snapshot=CodexTranscriptSnapshot(
            thread_hash="b" * 64,
            last_turn_id="turn-secret",
            messages=(TranscriptMessage("assistant", "raw transcript secret", None),),
            byte_count=21,
            truncated=False,
            captured_at="2026-07-14T06:00:00Z",
        ),
        now=utc(1),
    )

    diagnostics = repr(journal.diagnostics(job.job_id))
    assert "raw-thread-id" not in diagnostics
    assert "raw transcript secret" not in diagnostics
    assert "worker-secret-token" not in diagnostics
    assert "turn-secret" not in diagnostics
