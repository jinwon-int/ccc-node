"""Runtime contract for the human-gated Codex Wiki candidate sink (#465)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import test_distill_worker as fixtures

from telegram_bot.core.bot import TelegramBot
from telegram_bot.memory.distill_journal import DistillJournal
from telegram_bot.memory.distill_types import DistillWikiSinkStatus
from telegram_bot.memory.distill_wiki_worker import CodexDistillWikiSinkWorker
from telegram_bot.memory.distill_worker import CodexDistillExtractionWorker


class WikiBackend:
    async def extract(self, extraction_input):  # type: ignore[no-untyped-def]
        return fixtures.wiki_output_for(extraction_input)


async def wiki_job(journal: DistillJournal):  # type: ignore[no-untyped-def]
    snapshot_done = fixtures.snapshot_done_job(
        journal,
        thread_id="thread-wiki-worker",
    )
    return await CodexDistillExtractionWorker(
        journal,
        WikiBackend(),
        usage_meter=None,
        owner_token="extract-worker",
        wiki_enabled=True,
    ).extract_once(job_id=snapshot_done.job_id)


@pytest.mark.anyio
async def test_worker_queues_validated_candidate_for_human_review(tmp_path: Path) -> None:
    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    job = await wiki_job(journal)
    queue = tmp_path / "wiki-candidates"
    worker = CodexDistillWikiSinkWorker(
        journal, queue_dir=queue, owner_token="wiki-worker"
    )

    result = await worker.write_once(job_id=job.job_id)

    assert result.wiki_sink_status is DistillWikiSinkStatus.DONE
    record = json.loads((queue / f"{job.job_id}.json").read_text())
    assert record["review_status"] == "pending"
    assert record["candidates"][0]["suggested_path"].startswith("pages/nodes/")


@pytest.mark.anyio
async def test_ten_workers_create_one_record(tmp_path: Path) -> None:
    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    job = await wiki_job(journal)
    queue = tmp_path / "wiki-candidates"
    workers = tuple(
        CodexDistillWikiSinkWorker(
            journal, queue_dir=queue, owner_token=f"wiki-{index}"
        )
        for index in range(10)
    )

    await asyncio.gather(*(worker.write_once(job_id=job.job_id) for worker in workers))

    assert journal.get(job.job_id).wiki_sink_status is DistillWikiSinkStatus.DONE
    assert len(list(queue.glob("*.json"))) == 1


@pytest.mark.anyio
async def test_io_failure_retries_without_reextracting(tmp_path: Path) -> None:
    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    job = await wiki_job(journal)
    worker = CodexDistillWikiSinkWorker(
        journal, queue_dir=tmp_path / "wiki-candidates", owner_token="wiki-worker"
    )

    with patch(
        "telegram_bot.memory.distill_wiki_worker.CodexWikiCandidateSink.write",
        side_effect=OSError("raw path must not leak"),
    ):
        failed = await worker.write_once(job_id=job.job_id)

    assert failed.wiki_sink_status is DistillWikiSinkStatus.RETRYABLE_FAILED
    assert failed.error_code == "wiki_sink_io_failed"
    assert failed.extraction_attempts == job.extraction_attempts
    assert "raw path" not in repr(journal.diagnostics(job.job_id))

    completed = await worker.write_once(job_id=job.job_id)
    assert completed.wiki_sink_status is DistillWikiSinkStatus.DONE
    assert completed.extraction_attempts == job.extraction_attempts


@pytest.mark.anyio
async def test_collision_is_terminal_and_does_not_overwrite_review(tmp_path: Path) -> None:
    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    job = await wiki_job(journal)
    queue = tmp_path / "wiki-candidates"
    queue.mkdir(mode=0o700)
    path = queue / f"{job.job_id}.json"
    path.write_text('{"review_status":"accepted"}')
    path.chmod(0o600)
    worker = CodexDistillWikiSinkWorker(
        journal, queue_dir=queue, owner_token="wiki-worker"
    )

    failed = await worker.write_once(job_id=job.job_id)

    assert failed.wiki_sink_status is DistillWikiSinkStatus.TERMINAL_FAILED
    assert failed.error_code == "wiki_sink_record_collision"
    assert path.read_text() == '{"review_status":"accepted"}'


@pytest.mark.anyio
async def test_lifecycle_loop_drives_pending_wiki_work(tmp_path: Path) -> None:
    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    job = await wiki_job(journal)
    worker = CodexDistillWikiSinkWorker(
        journal, queue_dir=tmp_path / "wiki-candidates", owner_token="wiki-loop"
    )
    bot = TelegramBot.__new__(TelegramBot)
    bot._distill_journal = journal
    bot._distill_wiki_sink_worker = worker
    bot._config = SimpleNamespace(distill_extraction_poll_interval=0.02)
    stop = asyncio.Event()
    task = asyncio.create_task(bot._distill_wiki_sink_loop(stop))
    try:
        deadline = asyncio.get_running_loop().time() + 2
        while (
            journal.get(job.job_id).wiki_sink_status is not DistillWikiSinkStatus.DONE
            and asyncio.get_running_loop().time() < deadline
        ):
            await asyncio.sleep(0.02)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=2)

    assert journal.get(job.job_id).wiki_sink_status is DistillWikiSinkStatus.DONE

