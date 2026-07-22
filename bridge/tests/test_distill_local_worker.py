"""Runtime contract for routed Codex local-sink work (#465)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from test_distill_local_journal import extracted_job

from telegram_bot.core.bot import TelegramBot
from telegram_bot.memory.distill_journal import DistillJournal
from telegram_bot.memory.distill_local_worker import CodexDistillLocalSinkWorker
from telegram_bot.memory.distill_types import DistillLocalSinkStatus


@pytest.mark.anyio
async def test_worker_routes_only_to_the_jobs_opaque_scope(tmp_path: Path) -> None:
    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    job = await extracted_job(journal)
    audience_root = tmp_path / "audiences"
    worker = CodexDistillLocalSinkWorker(
        journal,
        audience_root=audience_root,
        owner_token="local-worker",
    )

    result = await worker.write_once(job_id=job.job_id)

    assert result.local_sink_status is DistillLocalSinkStatus.DONE
    assert result.local_sink_attempts == 1
    state_dir = audience_root / job.memory_scope / "state"  # type: ignore[operator]
    facts = [
        json.loads(line) for line in (state_dir / "memory-facts.jsonl").read_text().splitlines()
    ]
    assert len(facts) == 1
    assert facts[0]["audience"] == "private"
    assert facts[0]["source"]["thread_hash"] == job.thread_hash
    assert (state_dir / "resume.md").is_file()
    assert not (audience_root / "shared" / "state" / "memory-facts.jsonl").exists()


@pytest.mark.anyio
async def test_ten_workers_replay_one_local_mutation(tmp_path: Path) -> None:
    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    job = await extracted_job(journal)
    audience_root = tmp_path / "audiences"
    workers = tuple(
        CodexDistillLocalSinkWorker(
            journal,
            audience_root=audience_root,
            owner_token=f"local-{index}",
        )
        for index in range(10)
    )

    await asyncio.gather(*(worker.write_once(job_id=job.job_id) for worker in workers))

    persisted = journal.get(job.job_id)
    assert persisted.local_sink_status is DistillLocalSinkStatus.DONE
    assert persisted.local_sink_attempts == 1
    facts_path = audience_root / str(job.memory_scope) / "state" / "memory-facts.jsonl"
    assert len(facts_path.read_text().splitlines()) == 1


@pytest.mark.anyio
async def test_io_failure_retries_without_reextracting(tmp_path: Path) -> None:
    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    job = await extracted_job(journal)
    extraction_attempts = job.extraction_attempts
    worker = CodexDistillLocalSinkWorker(
        journal,
        audience_root=tmp_path / "audiences",
        owner_token="local-worker",
    )

    with patch(
        "telegram_bot.memory.distill_local_worker.CodexLocalMemorySink.write",
        side_effect=OSError("sensitive path detail"),
    ):
        failed = await worker.write_once(job_id=job.job_id)

    assert failed.local_sink_status is DistillLocalSinkStatus.RETRYABLE_FAILED
    assert failed.error_code == "local_sink_io_failed"
    assert failed.extraction_attempts == extraction_attempts
    assert "sensitive" not in repr(journal.diagnostics(job.job_id))

    completed = await worker.write_once(job_id=job.job_id)
    assert completed.local_sink_status is DistillLocalSinkStatus.DONE
    assert completed.extraction_attempts == extraction_attempts


@pytest.mark.anyio
async def test_index_failure_retries_body_free_without_duplicating_fact(
    tmp_path: Path,
) -> None:
    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    job = await extracted_job(journal)
    indexer = tmp_path / "indexer.sh"
    indexer.write_text(
        "#!/bin/sh\necho RAW_INDEX_ERROR_MUST_NOT_LEAK >&2\nexit 9\n"
    )
    indexer.chmod(0o700)
    audience_root = tmp_path / "audiences"
    worker = CodexDistillLocalSinkWorker(
        journal,
        audience_root=audience_root,
        owner_token="local-worker",
        indexer_path=indexer,
        environment={
            "HOME": str(tmp_path / "home"),
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "TELEGRAM_BOT_TOKEN": "RAW_TELEGRAM_TOKEN_MUST_NOT_CROSS",
            "CODEX_API_KEY": "RAW_CODEX_KEY_MUST_NOT_CROSS",
            "CCC_HONCHO_TOKEN": "RAW_HONCHO_TOKEN_MUST_NOT_CROSS",
        },
    )

    failed = await worker.write_once(job_id=job.job_id)

    assert failed.local_sink_status is DistillLocalSinkStatus.RETRYABLE_FAILED
    assert failed.error_code == "local_sink_index_failed"
    assert "RAW_INDEX_ERROR" not in repr(journal.diagnostics(job.job_id))
    facts_path = audience_root / str(job.memory_scope) / "state" / "memory-facts.jsonl"
    assert len(facts_path.read_text().splitlines()) == 1

    indexer.write_text(
        "#!/bin/sh\n"
        "[ -z \"${TELEGRAM_BOT_TOKEN:-}${CODEX_API_KEY:-}${CCC_HONCHO_TOKEN:-}\" ] || exit 7\n"
        "[ \"${CCC_WIKI_MEMORY_ENABLED:-}\" = 0 ] || exit 8\n"
        "[ \"${CCC_HONCHO_MEMORY_ENABLED:-}\" = 0 ] || exit 9\n"
        "exit 0\n"
    )
    indexer.chmod(0o700)
    completed = await worker.write_once(job_id=job.job_id)

    assert completed.local_sink_status is DistillLocalSinkStatus.DONE
    assert len(facts_path.read_text().splitlines()) == 1


@pytest.mark.anyio
async def test_unsafe_indexer_is_terminal_and_never_executed(tmp_path: Path) -> None:
    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    job = await extracted_job(journal)
    target = tmp_path / "unsafe-target.sh"
    marker = tmp_path / "must-not-exist"
    target.write_text(f"#!/bin/sh\ntouch {marker}\n")
    target.chmod(0o700)
    indexer = tmp_path / "indexer.sh"
    indexer.symlink_to(target)
    worker = CodexDistillLocalSinkWorker(
        journal,
        audience_root=tmp_path / "audiences",
        owner_token="local-worker",
        indexer_path=indexer,
    )

    failed = await worker.write_once(job_id=job.job_id)

    assert failed.local_sink_status is DistillLocalSinkStatus.TERMINAL_FAILED
    assert failed.error_code == "local_sink_index_unsafe"
    assert not marker.exists()


@pytest.mark.anyio
async def test_missing_indexer_keeps_durable_fact_retryable(tmp_path: Path) -> None:
    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    job = await extracted_job(journal)
    audience_root = tmp_path / "audiences"
    worker = CodexDistillLocalSinkWorker(
        journal,
        audience_root=audience_root,
        owner_token="local-worker",
        indexer_path=tmp_path / "not-installed-indexer.sh",
    )

    failed = await worker.write_once(job_id=job.job_id)

    assert failed.local_sink_status is DistillLocalSinkStatus.RETRYABLE_FAILED
    assert failed.error_code == "local_sink_index_failed"
    facts_path = audience_root / str(job.memory_scope) / "state" / "memory-facts.jsonl"
    assert len(facts_path.read_text().splitlines()) == 1


@pytest.mark.anyio
async def test_unsafe_scope_path_fails_terminal_without_following_symlink(
    tmp_path: Path,
) -> None:
    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    job = await extracted_job(journal)
    audience_root = tmp_path / "audiences"
    audience_root.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir()
    (audience_root / str(job.memory_scope)).symlink_to(outside, target_is_directory=True)
    worker = CodexDistillLocalSinkWorker(
        journal,
        audience_root=audience_root,
        owner_token="local-worker",
    )

    failed = await worker.write_once(job_id=job.job_id)

    assert failed.local_sink_status is DistillLocalSinkStatus.TERMINAL_FAILED
    assert failed.error_code == "local_sink_path_unsafe"
    assert list(outside.iterdir()) == []


@pytest.mark.anyio
async def test_lifecycle_loop_drives_pending_local_work(tmp_path: Path) -> None:
    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    job = await extracted_job(journal)
    worker = CodexDistillLocalSinkWorker(
        journal,
        audience_root=tmp_path / "audiences",
        owner_token="loop-worker",
    )
    bot = TelegramBot.__new__(TelegramBot)
    bot._distill_journal = journal
    bot._distill_local_sink_worker = worker
    bot._config = SimpleNamespace(distill_extraction_poll_interval=0.02)
    stop = asyncio.Event()
    task = asyncio.create_task(bot._distill_local_sink_loop(stop))
    try:
        deadline = asyncio.get_running_loop().time() + 2
        while (
            journal.get(job.job_id).local_sink_status is not DistillLocalSinkStatus.DONE
            and asyncio.get_running_loop().time() < deadline
        ):
            await asyncio.sleep(0.02)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=2)

    assert journal.get(job.job_id).local_sink_status is DistillLocalSinkStatus.DONE
