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
from telegram_bot.memory.distill_local_worker import (
    CodexDistillLocalSinkWorker,
    GlobalLocalMemoryRoute,
)
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


@pytest.mark.anyio
async def test_successful_sink_kicks_scoped_nunchi_feed(tmp_path: Path) -> None:
    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    job = await extracted_job(journal)
    marker = tmp_path / "kick-env"
    feed = tmp_path / "piri-feed.sh"
    feed.write_text(
        "#!/bin/sh\n"
        "{\n"
        "echo \"child=${CCC_NUNCHI_SCOPED_CHILD:-}\"\n"
        "echo \"scope=${CCC_NUNCHI_AUDIENCE_SCOPE:-}\"\n"
        "echo \"kind=${CCC_NUNCHI_AUDIENCE_KIND:-}\"\n"
        "echo \"db=${NUNCHI_DB:-}\"\n"
        "echo \"sessions=${PIR_SESSIONS_DIR:-}\"\n"
        "echo \"token=${TELEGRAM_BOT_TOKEN:-}\"\n"
        f"}} > {marker}\n",
    )
    feed.chmod(0o700)
    audience_root = tmp_path / "audiences"
    worker = CodexDistillLocalSinkWorker(
        journal,
        audience_root=audience_root,
        owner_token="local-worker",
        nunchi_feed_path=feed,
        environment={
            "HOME": str(tmp_path / "home"),
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "TELEGRAM_BOT_TOKEN": "RAW_TELEGRAM_TOKEN_MUST_NOT_CROSS",
        },
    )

    result = await worker.write_once(job_id=job.job_id)

    assert result.local_sink_status is DistillLocalSinkStatus.DONE
    if worker._nunchi_tasks:
        await asyncio.gather(*worker._nunchi_tasks)
    env = dict(
        line.split("=", 1) for line in marker.read_text().splitlines()
    )
    assert env["child"] == "1"
    assert env["scope"] == "private-0123456789abcdef0123456789abcdef"
    assert env["kind"] == "private"
    assert env["db"] == str(
        audience_root
        / "private-0123456789abcdef0123456789abcdef"
        / "nunchi"
        / "facts.db"
    )
    assert env["sessions"] == str(
        audience_root
        / "private-0123456789abcdef0123456789abcdef"
        / "piri"
        / "sessions"
    )
    assert env["token"] == ""


@pytest.mark.anyio
async def test_missing_nunchi_feed_skips_kick_and_sink_still_done(
    tmp_path: Path,
) -> None:
    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    job = await extracted_job(journal)
    worker = CodexDistillLocalSinkWorker(
        journal,
        audience_root=tmp_path / "audiences",
        owner_token="local-worker",
        nunchi_feed_path=tmp_path / "absent" / "piri-feed.sh",
    )

    result = await worker.write_once(job_id=job.job_id)

    assert result.local_sink_status is DistillLocalSinkStatus.DONE
    if worker._nunchi_tasks:
        await asyncio.gather(*worker._nunchi_tasks)
    assert not (tmp_path / "absent").exists()


@pytest.mark.anyio
async def test_unsafe_nunchi_feed_is_never_executed(tmp_path: Path) -> None:
    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    job = await extracted_job(journal)
    marker = tmp_path / "must-not-exist"
    target = tmp_path / "unsafe-target.sh"
    target.write_text(f"#!/bin/sh\ntouch {marker}\n")
    target.chmod(0o700)
    feed = tmp_path / "piri-feed.sh"
    feed.symlink_to(target)
    worker = CodexDistillLocalSinkWorker(
        journal,
        audience_root=tmp_path / "audiences",
        owner_token="local-worker",
        nunchi_feed_path=feed,
    )

    result = await worker.write_once(job_id=job.job_id)

    assert result.local_sink_status is DistillLocalSinkStatus.DONE
    if worker._nunchi_tasks:
        await asyncio.gather(*worker._nunchi_tasks)
    assert not marker.exists()


@pytest.mark.anyio
async def test_failing_nunchi_feed_never_affects_the_sink_job(
    tmp_path: Path,
) -> None:
    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    job = await extracted_job(journal)
    feed = tmp_path / "piri-feed.sh"
    feed.write_text("#!/bin/sh\nexit 9\n")
    feed.chmod(0o700)
    worker = CodexDistillLocalSinkWorker(
        journal,
        audience_root=tmp_path / "audiences",
        owner_token="local-worker",
        nunchi_feed_path=feed,
    )

    result = await worker.write_once(job_id=job.job_id)

    assert result.local_sink_status is DistillLocalSinkStatus.DONE
    if worker._nunchi_tasks:
        await asyncio.gather(*worker._nunchi_tasks)


# --- #2296: node-global route for off/curated bridge memory modes -----------


def _global_route(tmp_path: Path) -> GlobalLocalMemoryRoute:
    return GlobalLocalMemoryRoute(
        state_dir=tmp_path / "claude" / "state",
        cache_dir=tmp_path / "claude" / "hooks" / "cache",
        memory_dir=tmp_path / "claude" / "memories",
    )


def test_worker_requires_a_route_and_absolute_global_paths(tmp_path: Path) -> None:
    journal = DistillJournal(tmp_path / "journal")
    with pytest.raises(ValueError, match="audience root or global route"):
        CodexDistillLocalSinkWorker(journal)
    with pytest.raises(ValueError, match="absolute"):
        GlobalLocalMemoryRoute(state_dir=Path("relative/state"))


@pytest.mark.anyio
async def test_global_route_replays_unroutable_job_into_node_state_once(
    tmp_path: Path,
) -> None:
    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    job = await extracted_job(journal, memory_audience=None, memory_scope=None)
    assert job.local_sink_status is DistillLocalSinkStatus.UNROUTABLE
    route = _global_route(tmp_path)
    worker = CodexDistillLocalSinkWorker(
        journal,
        global_route=route,
        owner_token="global-worker",
    )
    assert worker.global_route_enabled is True

    result = await worker.write_once(job_id=job.job_id)

    assert result.local_sink_status is DistillLocalSinkStatus.DONE
    assert result.local_sink_attempts == 1
    facts = [
        json.loads(line)
        for line in (route.state_dir / "memory-facts.jsonl").read_text().splitlines()
    ]
    assert len(facts) == 1
    assert facts[0]["privacy"] == "private"
    assert facts[0]["source"]["job_id"] == job.job_id
    assert (route.state_dir / "resume.md").is_file()

    # Replay is idempotent: a second pass (or a second worker) is a no-op.
    again = await CodexDistillLocalSinkWorker(
        journal, global_route=route, owner_token="other"
    ).write_once(job_id=job.job_id)
    assert again.local_sink_status is DistillLocalSinkStatus.DONE
    assert again.local_sink_attempts == 1
    assert len((route.state_dir / "memory-facts.jsonl").read_text().splitlines()) == 1


@pytest.mark.anyio
async def test_global_replay_never_regresses_a_newer_resume(tmp_path: Path) -> None:
    """Replayed orphans run in journal order; an older one must not win resume.md."""

    import os

    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    job = await extracted_job(journal, memory_audience=None, memory_scope=None)
    route = _global_route(tmp_path)
    route.state_dir.mkdir(parents=True, mode=0o700)
    resume = route.state_dir / "resume.md"
    resume.write_text("- 마지막 작업: newer CLI session\n")
    resume.chmod(0o600)  # mtime is now, after the fixture's 2026-07-16 extraction

    result = await CodexDistillLocalSinkWorker(
        journal, global_route=route, owner_token="global-worker"
    ).write_once(job_id=job.job_id)

    assert result.local_sink_status is DistillLocalSinkStatus.DONE
    assert resume.read_text() == "- 마지막 작업: newer CLI session\n"
    assert len((route.state_dir / "memory-facts.jsonl").read_text().splitlines()) == 1

    # An older resume.md is replaced as usual.
    other_journal = DistillJournal(tmp_path / "journal-2")
    other_journal.initialize()
    second = await extracted_job(other_journal, memory_audience=None, memory_scope=None)
    os.utime(resume, (0, 0))
    await CodexDistillLocalSinkWorker(
        other_journal, global_route=route, owner_token="global-worker"
    ).write_once(job_id=second.job_id)
    assert "ccc-node:distill" in resume.read_text()


@pytest.mark.anyio
async def test_audience_worker_never_writes_route_free_job_globally(
    tmp_path: Path,
) -> None:
    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    job = await extracted_job(journal, memory_audience=None, memory_scope=None)
    worker = CodexDistillLocalSinkWorker(
        journal,
        audience_root=tmp_path / "audiences",
        owner_token="audience-worker",
    )
    assert worker.global_route_enabled is False

    result = await worker.write_once(job_id=job.job_id)

    assert result.local_sink_status is DistillLocalSinkStatus.UNROUTABLE
    assert not (tmp_path / "audiences").exists()
    assert not (tmp_path / "claude").exists()


@pytest.mark.anyio
async def test_global_only_worker_leaves_audience_routed_job_pending(
    tmp_path: Path,
) -> None:
    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    job = await extracted_job(journal)
    indexer = tmp_path / "ccc-memory-index.sh"
    indexer.write_text("#!/bin/sh\nexit 0\n")
    indexer.chmod(0o700)
    route = _global_route(tmp_path)
    worker = CodexDistillLocalSinkWorker(
        journal,
        global_route=route,
        owner_token="global-worker",
        indexer_path=indexer,
    )

    result = await worker.write_once(job_id=job.job_id)

    assert result.local_sink_status is DistillLocalSinkStatus.PENDING
    assert not (route.state_dir / "memory-facts.jsonl").exists()
    # No audience root: an audience refresh fails closed instead of guessing.
    with pytest.raises(RuntimeError, match="local_sink_index_unsafe"):
        await worker.refresh_route(audience="shared", scope="shared")


@pytest.mark.anyio
async def test_global_index_refresh_targets_node_state_without_audience_env(
    tmp_path: Path,
) -> None:
    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    job = await extracted_job(journal, memory_audience=None, memory_scope=None)
    marker = tmp_path / "index-env"
    indexer = tmp_path / "ccc-memory-index.sh"
    indexer.write_text(f"#!/bin/sh\nenv > {marker}\nexit 0\n")
    indexer.chmod(0o700)
    feed_marker = tmp_path / "feed-ran"
    feed = tmp_path / "piri-feed.sh"
    feed.write_text(f"#!/bin/sh\ntouch {feed_marker}\n")
    feed.chmod(0o700)
    route = _global_route(tmp_path)
    worker = CodexDistillLocalSinkWorker(
        journal,
        global_route=route,
        owner_token="global-worker",
        indexer_path=indexer,
        nunchi_feed_path=feed,
        environment={
            "HOME": str(tmp_path / "home"),
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "CCC_WIKI_MEMORY_ENABLED": "1",
            "CCC_MEMORY_EMBED_CMD": "RAW_EMBED_CMD_MUST_NOT_CROSS",
            "CCC_MEMORY_AUDIENCE_SCOPED": "1",
            "TELEGRAM_BOT_TOKEN": "RAW_TELEGRAM_TOKEN_MUST_NOT_CROSS",
        },
    )

    result = await worker.write_once(job_id=job.job_id)

    assert result.local_sink_status is DistillLocalSinkStatus.DONE
    if worker._nunchi_tasks:
        await asyncio.gather(*worker._nunchi_tasks)
    env = dict(
        line.split("=", 1) for line in marker.read_text().splitlines() if "=" in line
    )
    assert env["CCC_STATE_DIR"] == str(route.state_dir)
    assert env["CCC_MEMORY_INDEX_DB"] == str(route.state_dir / "memory-index.sqlite")
    assert env["CCC_MEMORY_FACTS_FILE"] == str(route.state_dir / "memory-facts.jsonl")
    assert env["CCC_MEMORY_CACHE_DIR"] == str(route.cache_dir)
    assert env["CCC_MEMORY_DIR"] == str(route.memory_dir)
    # The node's own Wiki policy is kept (an audience refresh forces it off).
    assert env["CCC_WIKI_MEMORY_ENABLED"] == "1"
    assert "CCC_MEMORY_AUDIENCE_SCOPED" not in env
    assert "CCC_MEMORY_SCOPE" not in env
    assert "CCC_MEMORY_EMBED_CMD" not in env
    assert "TELEGRAM_BOT_TOKEN" not in env
    # Global nunchi is fed from the journal by ingest-cron.sh, never kicked here.
    assert not feed_marker.exists()


@pytest.mark.anyio
async def test_global_index_failure_retries_without_duplicating_fact(
    tmp_path: Path,
) -> None:
    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    job = await extracted_job(journal, memory_audience=None, memory_scope=None)
    indexer = tmp_path / "ccc-memory-index.sh"
    indexer.write_text("#!/bin/sh\nexit 9\n")
    indexer.chmod(0o700)
    route = _global_route(tmp_path)
    worker = CodexDistillLocalSinkWorker(
        journal,
        global_route=route,
        owner_token="global-worker",
        indexer_path=indexer,
    )

    failed = await worker.write_once(job_id=job.job_id)
    assert failed.local_sink_status is DistillLocalSinkStatus.RETRYABLE_FAILED
    assert failed.error_code == "local_sink_index_failed"

    indexer.write_text("#!/bin/sh\nexit 0\n")
    indexer.chmod(0o700)
    completed = await worker.write_once(job_id=job.job_id)
    assert completed.local_sink_status is DistillLocalSinkStatus.DONE
    assert len((route.state_dir / "memory-facts.jsonl").read_text().splitlines()) == 1


async def _drive_local_sink_loop(
    worker: CodexDistillLocalSinkWorker,
    journal: DistillJournal,
    *,
    until=None,
) -> None:
    bot = TelegramBot.__new__(TelegramBot)
    bot._distill_journal = journal
    bot._distill_local_sink_worker = worker
    bot._config = SimpleNamespace(distill_extraction_poll_interval=0.02)
    stop = asyncio.Event()
    task = asyncio.create_task(bot._distill_local_sink_loop(stop))
    try:
        deadline = asyncio.get_running_loop().time() + 2
        while asyncio.get_running_loop().time() < deadline:
            if until is not None and until():
                break
            await asyncio.sleep(0.02)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=2)


@pytest.mark.anyio
async def test_lifecycle_loop_adopts_unroutable_jobs_only_with_global_route(
    tmp_path: Path,
) -> None:
    global_journal = DistillJournal(tmp_path / "global-journal")
    global_journal.initialize()
    replay = await extracted_job(global_journal, memory_audience=None, memory_scope=None)
    route = _global_route(tmp_path)
    await _drive_local_sink_loop(
        CodexDistillLocalSinkWorker(global_journal, global_route=route, owner_token="g"),
        global_journal,
        until=lambda: global_journal.get(replay.job_id).local_sink_status
        is DistillLocalSinkStatus.DONE,
    )
    assert global_journal.get(replay.job_id).local_sink_status is DistillLocalSinkStatus.DONE
    assert (route.state_dir / "memory-facts.jsonl").is_file()

    audience_journal = DistillJournal(tmp_path / "audience-journal")
    audience_journal.initialize()
    orphan = await extracted_job(audience_journal, memory_audience=None, memory_scope=None)
    calls: list[str] = []
    audience_worker = CodexDistillLocalSinkWorker(
        audience_journal, audience_root=tmp_path / "audiences", owner_token="a"
    )
    original = audience_worker.write_once

    async def counting_write_once(*, job_id: str):
        calls.append(job_id)
        return await original(job_id=job_id)

    audience_worker.write_once = counting_write_once  # type: ignore[method-assign]
    await _drive_local_sink_loop(
        audience_worker, audience_journal, until=lambda: False
    )
    # Audience-scoped composition never even offers UNROUTABLE work.
    assert calls == []
    assert (
        audience_journal.get(orphan.job_id).local_sink_status
        is DistillLocalSinkStatus.UNROUTABLE
    )
