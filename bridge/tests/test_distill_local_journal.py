"""Journal contract for independently replayable Codex local-sink work (#465)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import test_distill_worker as fixtures

from telegram_bot.memory.distill_journal import DistillJournal
from telegram_bot.memory.distill_types import (
    DistillJob,
    DistillLocalSinkStatus,
    DistillTrigger,
)
from telegram_bot.memory.distill_worker import CodexDistillExtractionWorker


PRIVATE_SCOPE = "private-0123456789abcdef0123456789abcdef"


async def extracted_job(
    journal: DistillJournal,
    *,
    memory_audience: str | None = "private",
    memory_scope: str | None = PRIVATE_SCOPE,
    wiki_enabled: bool = True,
    honcho_enabled: bool = True,
) -> DistillJob:
    queued = journal.enqueue_once(
        provider="codex",
        thread_id="thread-local-journal",
        trigger=DistillTrigger.NEW_COMMAND,
        memory_audience=memory_audience,
        memory_scope=memory_scope,
    )
    claimed = journal.claim(
        queued.job_id,
        owner_token="snapshot-worker",
        now=fixtures.utc(),
    )
    assert claimed is not None
    journal.mark_snapshot_done(
        queued.job_id,
        owner_token="snapshot-worker",
        lease_epoch=claimed.lease_epoch,
        snapshot=fixtures.snapshot("thread-local-journal"),
        now=fixtures.utc(1),
    )
    return await CodexDistillExtractionWorker(
        journal,
        fixtures.SuccessfulBackend(),
        usage_meter=None,
        owner_token="extract-worker",
        wiki_enabled=wiki_enabled,
        honcho_enabled=honcho_enabled,
    ).extract_once(job_id=queued.job_id)


@pytest.mark.parametrize(
    ("audience", "scope"),
    [
        ("private", None),
        (None, PRIVATE_SCOPE),
        ("shared", PRIVATE_SCOPE),
        ("private", "shared"),
        ("private", "private-raw-user-id"),
        ("public", "shared"),
    ],
)
def test_enqueue_rejects_incomplete_or_non_opaque_audience_routes(
    tmp_path: Path, audience: str | None, scope: str | None
) -> None:
    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()

    with pytest.raises(ValueError, match="memory audience route"):
        journal.enqueue_once(
            provider="codex",
            thread_id="thread-route-invalid",
            trigger=DistillTrigger.NEW_COMMAND,
            memory_audience=audience,
            memory_scope=scope,
        )


def test_enqueue_persists_only_opaque_route_and_rejects_route_collision(
    tmp_path: Path,
) -> None:
    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    job = journal.enqueue_once(
        provider="codex",
        thread_id="thread-route",
        trigger=DistillTrigger.NEW_COMMAND,
        memory_audience="private",
        memory_scope=PRIVATE_SCOPE,
    )

    persisted = journal.get(job.job_id)
    assert persisted.memory_audience == "private"
    assert persisted.memory_scope == PRIVATE_SCOPE
    record = journal.job_path(job.job_id).read_text()
    assert "telegram-user-123" not in record
    with pytest.raises(RuntimeError, match="route collision"):
        journal.enqueue_once(
            provider="codex",
            thread_id="thread-route",
            trigger=DistillTrigger.NEW_COMMAND,
            memory_audience="shared",
            memory_scope="shared",
        )


@pytest.mark.anyio
async def test_extraction_marks_routed_work_pending_and_missing_route_unroutable(
    tmp_path: Path,
) -> None:
    routed_journal = DistillJournal(tmp_path / "routed")
    routed_journal.initialize()
    routed = await extracted_job(routed_journal)
    assert routed.local_sink_status is DistillLocalSinkStatus.PENDING

    legacy_journal = DistillJournal(tmp_path / "legacy")
    legacy_journal.initialize()
    unroutable = await extracted_job(
        legacy_journal,
        memory_audience=None,
        memory_scope=None,
    )
    assert unroutable.local_sink_status is DistillLocalSinkStatus.UNROUTABLE
    assert legacy_journal.claim_local_sink(unroutable.job_id, owner_token="sink-worker") is None


@pytest.mark.anyio
async def test_local_sink_lease_is_fenced_recoverable_and_independent(
    tmp_path: Path,
) -> None:
    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    done = await extracted_job(journal)
    extraction_attempts = done.extraction_attempts
    output_hash = done.extraction_output_hash

    claims = await asyncio.gather(
        *(
            asyncio.to_thread(
                journal.claim_local_sink,
                done.job_id,
                owner_token=f"sink-{index}",
                now=fixtures.utc(2),
                lease_seconds=10,
            )
            for index in range(10)
        )
    )
    winners = [claim for claim in claims if claim is not None]
    assert len(winners) == 1
    claimed = winners[0]
    assert claimed.local_sink_status is DistillLocalSinkStatus.RUNNING
    assert claimed.local_sink_attempts == 1
    assert journal.recover_stale_running(now=fixtures.utc(5)) == 0
    assert journal.recover_stale_running(now=fixtures.utc(13)) == 1
    recovered = journal.get(done.job_id)
    assert recovered.local_sink_status is DistillLocalSinkStatus.RETRYABLE_FAILED
    assert recovered.error_code == "local_sink_lease_expired"
    assert recovered.extraction_attempts == extraction_attempts
    assert recovered.extraction_output_hash == output_hash


@pytest.mark.anyio
async def test_local_failure_retries_without_losing_extraction_then_completes(
    tmp_path: Path,
) -> None:
    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    done = await extracted_job(journal)
    claimed = journal.claim_local_sink(
        done.job_id,
        owner_token="sink-a",
        now=fixtures.utc(2),
    )
    assert claimed is not None
    failed = journal.mark_local_sink_retryable_failed(
        done.job_id,
        owner_token="sink-a",
        lease_epoch=claimed.local_sink_lease_epoch,
        error_code="local_sink_io_failed",
        now=fixtures.utc(3),
    )
    assert failed.local_sink_status is DistillLocalSinkStatus.RETRYABLE_FAILED
    assert failed.extraction_output == done.extraction_output
    assert failed.extraction_attempts == done.extraction_attempts

    retried = journal.claim_local_sink(
        done.job_id,
        owner_token="sink-b",
        now=fixtures.utc(4),
    )
    assert retried is not None
    with pytest.raises(RuntimeError, match="owner or lease"):
        journal.mark_local_sink_done(
            done.job_id,
            owner_token="sink-a",
            lease_epoch=claimed.local_sink_lease_epoch,
            now=fixtures.utc(5),
        )
    completed = journal.mark_local_sink_done(
        done.job_id,
        owner_token="sink-b",
        lease_epoch=retried.local_sink_lease_epoch,
        now=fixtures.utc(5),
    )
    assert completed.local_sink_status is DistillLocalSinkStatus.DONE
    assert completed.error_code is None
    assert journal.claim_local_sink(done.job_id, owner_token="sink-c", now=fixtures.utc(6)) is None


@pytest.mark.anyio
async def test_local_max_attempts_is_terminal_and_diagnostics_are_body_free(
    tmp_path: Path,
) -> None:
    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    done = await extracted_job(journal)
    first = journal.claim_local_sink(
        done.job_id,
        owner_token="sink-a",
        max_attempts=1,
    )
    assert first is not None
    journal.mark_local_sink_retryable_failed(
        done.job_id,
        owner_token="sink-a",
        lease_epoch=first.local_sink_lease_epoch,
        error_code="local_sink_io_failed",
    )
    assert (
        journal.claim_local_sink(
            done.job_id,
            owner_token="sink-b",
            max_attempts=1,
        )
        is None
    )
    terminal = journal.get(done.job_id)
    assert terminal.local_sink_status is DistillLocalSinkStatus.TERMINAL_FAILED
    diagnostics = journal.diagnostics(done.job_id)
    assert diagnostics["local_sink_status"] == "terminal_failed"
    assert diagnostics["local_sink_attempts"] == 1
    assert PRIVATE_SCOPE not in repr(diagnostics)
    assert "A harmless fact was retained" not in repr(diagnostics)


@pytest.mark.anyio
async def test_pre_route_record_reopens_as_unroutable_without_mutation(
    tmp_path: Path,
) -> None:
    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    done = await extracted_job(journal)
    path = journal.job_path(done.job_id)
    record = json.loads(path.read_text())
    for key in (
        "memory_audience",
        "memory_scope",
        "local_sink_status",
        "local_sink_attempts",
        "local_sink_lease_epoch",
    ):
        record.pop(key, None)
    path.write_text(json.dumps(record, separators=(",", ":")))
    path.chmod(0o600)

    reopened = DistillJournal(journal.root)
    reopened.initialize()
    legacy = reopened.get(done.job_id)
    assert legacy.memory_audience is None
    assert legacy.memory_scope is None
    assert legacy.local_sink_status is DistillLocalSinkStatus.UNROUTABLE
    assert reopened.claim_local_sink(done.job_id, owner_token="sink-worker") is None
