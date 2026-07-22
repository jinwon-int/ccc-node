"""Journal contract for independently replayable Wiki candidate work (#465)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import test_distill_worker as fixtures
from test_distill_local_journal import extracted_job

from telegram_bot.memory.distill_journal import DistillJournal
from telegram_bot.memory.distill_types import (
    DistillLocalSinkStatus,
    DistillWikiSinkStatus,
)


@pytest.mark.anyio
async def test_extraction_marks_wiki_pending_or_explicitly_disabled(tmp_path: Path) -> None:
    enabled = DistillJournal(tmp_path / "enabled")
    enabled.initialize()
    pending = await extracted_job(enabled, wiki_enabled=True)
    assert pending.wiki_sink_status is DistillWikiSinkStatus.PENDING

    disabled = DistillJournal(tmp_path / "disabled")
    disabled.initialize()
    skipped = await extracted_job(disabled, wiki_enabled=False)
    assert skipped.wiki_sink_status is DistillWikiSinkStatus.DISABLED
    assert disabled.claim_wiki_sink(skipped.job_id, owner_token="wiki-worker") is None


@pytest.mark.anyio
async def test_wiki_lease_is_single_winner_recoverable_and_local_independent(
    tmp_path: Path,
) -> None:
    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    done = await extracted_job(journal)

    claims = await asyncio.gather(
        *(
            asyncio.to_thread(
                journal.claim_wiki_sink,
                done.job_id,
                owner_token=f"wiki-{index}",
                now=fixtures.utc(2),
                lease_seconds=10,
            )
            for index in range(10)
        )
    )
    winners = [claim for claim in claims if claim is not None]
    assert len(winners) == 1
    claimed = winners[0]
    assert claimed.wiki_sink_status is DistillWikiSinkStatus.RUNNING
    assert claimed.local_sink_status is DistillLocalSinkStatus.PENDING
    local_claimed = journal.claim_local_sink(
        done.job_id,
        owner_token="local-worker",
        now=fixtures.utc(2),
        lease_seconds=10,
    )
    assert local_claimed is not None
    assert local_claimed.wiki_sink_status is DistillWikiSinkStatus.RUNNING
    assert local_claimed.local_sink_status is DistillLocalSinkStatus.RUNNING
    honcho_claimed = journal.claim_honcho_sink(
        done.job_id,
        owner_token="honcho-worker",
        now=fixtures.utc(2),
        lease_seconds=10,
    )
    assert honcho_claimed is not None
    assert journal.recover_stale_running(now=fixtures.utc(5)) == 0
    assert journal.recover_stale_running(now=fixtures.utc(13)) == 3
    recovered = journal.get(done.job_id)
    assert recovered.wiki_sink_status is DistillWikiSinkStatus.RETRYABLE_FAILED
    assert recovered.local_sink_status is DistillLocalSinkStatus.RETRYABLE_FAILED
    assert recovered.error_code == "honcho_sink_lease_expired"


@pytest.mark.anyio
async def test_wiki_retry_then_complete_preserves_extraction(tmp_path: Path) -> None:
    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    done = await extracted_job(journal)
    first = journal.claim_wiki_sink(done.job_id, owner_token="wiki-a")
    assert first is not None
    failed = journal.mark_wiki_sink_retryable_failed(
        done.job_id,
        owner_token="wiki-a",
        lease_epoch=first.wiki_sink_lease_epoch,
        error_code="wiki_sink_io_failed",
    )
    assert failed.wiki_sink_status is DistillWikiSinkStatus.RETRYABLE_FAILED
    assert failed.extraction_output_hash == done.extraction_output_hash

    second = journal.claim_wiki_sink(done.job_id, owner_token="wiki-b")
    assert second is not None
    completed = journal.mark_wiki_sink_done(
        done.job_id,
        owner_token="wiki-b",
        lease_epoch=second.wiki_sink_lease_epoch,
    )
    assert completed.wiki_sink_status is DistillWikiSinkStatus.DONE
    diagnostics = journal.diagnostics(done.job_id)
    assert diagnostics["wiki_sink_status"] == "done"
    assert diagnostics["wiki_sink_attempts"] == 2
    assert "Keep candidates local" not in repr(diagnostics)
