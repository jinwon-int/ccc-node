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
    provider: str = "codex",
) -> DistillJob:
    queued = journal.enqueue_once(
        provider=provider,
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
async def test_global_route_adopts_unroutable_once_and_never_routed_work(
    tmp_path: Path,
) -> None:
    """#2296: the node-global sink replays orphaned UNROUTABLE jobs exactly once."""

    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    orphan = await extracted_job(journal, memory_audience=None, memory_scope=None)
    assert orphan.local_sink_status is DistillLocalSinkStatus.UNROUTABLE

    # Audience-only and explicitly audience-only claims leave it alone.
    assert journal.claim_local_sink(orphan.job_id, owner_token="aud") is None
    assert (
        journal.claim_local_sink(
            orphan.job_id, owner_token="aud", audience_routes=True, global_route=False
        )
        is None
    )
    assert journal.get(orphan.job_id).local_sink_status is DistillLocalSinkStatus.UNROUTABLE

    claimed = journal.claim_local_sink(
        orphan.job_id,
        owner_token="global",
        audience_routes=False,
        global_route=True,
        now=fixtures.utc(2),
    )
    assert claimed is not None
    assert claimed.local_sink_status is DistillLocalSinkStatus.RUNNING
    assert claimed.memory_audience is None and claimed.memory_scope is None
    # A route-free retry survives a reopen (the record validates on read).
    failed = journal.mark_local_sink_retryable_failed(
        orphan.job_id,
        owner_token="global",
        lease_epoch=claimed.local_sink_lease_epoch,
        error_code="local_sink_io_failed",
    )
    reopened = DistillJournal(journal.root)
    reopened.initialize()
    assert reopened.get(orphan.job_id) == failed
    assert journal.claim_local_sink(orphan.job_id, owner_token="aud") is None
    retried = journal.claim_local_sink(
        orphan.job_id, owner_token="global", audience_routes=False, global_route=True
    )
    assert retried is not None and retried.local_sink_attempts == 2
    done = journal.mark_local_sink_done(
        orphan.job_id,
        owner_token="global",
        lease_epoch=retried.local_sink_lease_epoch,
    )
    assert done.local_sink_status is DistillLocalSinkStatus.DONE
    # Idempotent: a DONE job is never claimed again by any route.
    assert (
        journal.claim_local_sink(
            orphan.job_id, owner_token="again", audience_routes=True, global_route=True
        )
        is None
    )

    routed_journal = DistillJournal(tmp_path / "routed")
    routed_journal.initialize()
    routed = await extracted_job(routed_journal)
    assert (
        routed_journal.claim_local_sink(
            routed.job_id, owner_token="global", audience_routes=False, global_route=True
        )
        is None
    )
    assert routed_journal.get(routed.job_id).local_sink_status is DistillLocalSinkStatus.PENDING


@pytest.mark.anyio
async def test_prune_keeps_unadopted_unroutable_jobs_while_global_route_exists(
    tmp_path: Path,
) -> None:
    """#2296: retention must not delete orphans before global adoption."""

    from datetime import datetime, timedelta, timezone

    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    orphan = await extracted_job(
        journal, memory_audience=None, memory_scope=None, wiki_enabled=False
    )
    assert orphan.local_sink_status is DistillLocalSinkStatus.UNROUTABLE
    later = datetime.now(timezone.utc) + timedelta(days=30)

    assert journal.prune_terminal_jobs(
        older_than_seconds=60, now=later, keep_unroutable=True
    ) == ()
    assert journal.get(orphan.job_id).local_sink_status is DistillLocalSinkStatus.UNROUTABLE

    # Once adopted (DONE) it is ordinary terminal work again.
    claimed = journal.claim_local_sink(
        orphan.job_id, owner_token="global", audience_routes=False, global_route=True
    )
    assert claimed is not None
    journal.mark_local_sink_done(
        orphan.job_id,
        owner_token="global",
        lease_epoch=claimed.local_sink_lease_epoch,
    )
    assert journal.prune_terminal_jobs(
        older_than_seconds=60, now=later, keep_unroutable=True
    ) == (orphan.job_id,)

    # Without a global route the historical behavior is unchanged.
    legacy = DistillJournal(tmp_path / "legacy")
    legacy.initialize()
    other = await extracted_job(
        legacy, memory_audience=None, memory_scope=None, wiki_enabled=False
    )
    assert legacy.prune_terminal_jobs(older_than_seconds=60, now=later) == (
        other.job_id,
    )


@pytest.mark.anyio
async def test_sweep_prune_keeps_orphans_only_with_global_sink(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from telegram_bot.core.bot import TelegramBot

    calls: list[dict[str, object]] = []

    class _Journal:
        def recover_stale_running(self):
            return 0

        def prune_terminal_jobs(self, **kwargs):
            calls.append(kwargs)
            return ()

        def list_jobs(self):
            return ()

    for worker, expected in (
        (SimpleNamespace(global_route_enabled=True), True),
        (SimpleNamespace(global_route_enabled=False), False),
        (None, False),
    ):
        bot = TelegramBot.__new__(TelegramBot)
        bot._distill_journal = _Journal()
        bot._distill_local_sink_worker = worker
        bot._config = SimpleNamespace(distill_job_retention_seconds=3600.0)
        calls.clear()
        await bot._distill_sweep_jobs(0.0)
        assert calls, "sweep did not prune"
        assert calls[-1].get("keep_unroutable", False) is expected


def test_route_free_local_sink_may_not_start_pending(tmp_path: Path) -> None:
    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    job = journal.enqueue_once(
        provider="codex",
        thread_id="thread-pending-routeless",
        trigger=DistillTrigger.NEW_COMMAND,
    )
    payload = job.to_dict()
    payload["local_sink_status"] = "pending"
    with pytest.raises(ValueError, match="requires a memory route"):
        DistillJob.from_dict(payload)


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
