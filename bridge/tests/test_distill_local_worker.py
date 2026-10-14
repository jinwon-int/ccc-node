"""Runtime contract for routed Codex local-sink work (#465)."""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import datetime
import json
import os
from pathlib import Path
import re
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from test_distill_local_journal import extracted_job
import test_distill_worker as fixtures

from telegram_bot.core.bot import TelegramBot
from telegram_bot.memory.distill_journal import DistillJournal
from telegram_bot.memory.distill_local_worker import (
    CodexDistillLocalSinkWorker,
    GlobalLocalMemoryRoute,
)
from telegram_bot.memory.distill_extraction import DistillExtractionOutput
from telegram_bot.memory.distill_types import DistillLocalSinkStatus, DistillTrigger
from telegram_bot.memory.distill_worker import CodexDistillExtractionWorker


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


class _ResumeBackend:
    """Successful extractor with a chosen (untrusted) distilled_at and resume."""

    def __init__(self, distilled_at: str, activity: str) -> None:
        self._distilled_at = distilled_at
        self._activity = activity

    async def extract(self, extraction_input):
        payload = fixtures.output_for(extraction_input).model_dump(mode="json")
        payload["provenance"]["distilled_at"] = self._distilled_at
        payload["resume"]["last_activity"] = self._activity
        payload["honcho"][0]["text"] = f"Fact from {self._activity}."
        return DistillExtractionOutput.model_validate(payload)


async def _global_job(
    journal: DistillJournal,
    *,
    thread_id: str,
    captured_at: str,
    distilled_at: str,
    activity: str,
):
    queued = journal.enqueue_once(
        provider="codex",
        thread_id=thread_id,
        trigger=DistillTrigger.NEW_COMMAND,
    )
    claimed = journal.claim(queued.job_id, owner_token="snap", now=fixtures.utc())
    assert claimed is not None
    journal.mark_snapshot_done(
        queued.job_id,
        owner_token="snap",
        lease_epoch=claimed.lease_epoch,
        snapshot=dataclasses.replace(
            fixtures.snapshot(thread_id), captured_at=captured_at
        ),
        now=fixtures.utc(1),
    )
    done = await CodexDistillExtractionWorker(
        journal,
        _ResumeBackend(distilled_at, activity),
        usage_meter=None,
        owner_token="extract",
        wiki_enabled=True,
    ).extract_once(job_id=queued.job_id)
    assert done.local_sink_status is DistillLocalSinkStatus.UNROUTABLE
    return done


@pytest.mark.anyio
async def test_global_replay_newest_capture_wins_resume_in_any_order(
    tmp_path: Path,
) -> None:
    """Replay runs in job-hash order and distilled_at is model-authored.

    Only the bridge-recorded snapshot capture time orders resume.md: the
    newest capture wins even when it is processed first and carries a stale
    distilled_at, and a far-future distilled_at on an old capture never
    suppresses anything.
    """

    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    jobs = {
        "old": await _global_job(
            journal,
            thread_id="thread-old",
            captured_at="2026-10-01T09:00:00Z",
            distilled_at="2099-01-01T00:00:00Z",
            activity="old session",
        ),
        "mid": await _global_job(
            journal,
            thread_id="thread-mid",
            captured_at="2026-10-05T09:00:00.250000Z",
            distilled_at="2026-10-05T00:00:00Z",
            activity="mid session",
        ),
        "new": await _global_job(
            journal,
            thread_id="thread-new",
            captured_at="2026-10-09T09:00:00Z",
            distilled_at="2020-01-01T00:00:00Z",
            activity="new session",
        ),
    }
    route = _global_route(tmp_path)
    resume = route.state_dir / "resume.md"
    worker = CodexDistillLocalSinkWorker(
        journal, global_route=route, owner_token="global-worker"
    )

    for name in ("new", "old", "mid"):
        result = await worker.write_once(job_id=jobs[name].job_id)
        assert result.local_sink_status is DistillLocalSinkStatus.DONE

    text = resume.read_text()
    assert "new session" in text
    assert "captured_at=2026-10-09T09:00:00.000000Z" in text.splitlines()[0]
    # Every job's facts are still merged.
    facts = (route.state_dir / "memory-facts.jsonl").read_text()
    assert all(f"Fact from {name} session." in facts for name in jobs)

    # Sweep order (sorted job ids) gives the same answer from scratch.
    other_route = _global_route(tmp_path / "sorted")
    other_journal = DistillJournal(tmp_path / "journal-sorted")
    other_journal.initialize()
    for name, captured in (
        ("old", "2026-10-01T09:00:00Z"),
        ("mid", "2026-10-05T09:00:00Z"),
        ("new", "2026-10-09T09:00:00Z"),
    ):
        await _global_job(
            other_journal,
            thread_id=f"thread-{name}",
            captured_at=captured,
            distilled_at="2026-10-13T00:00:00Z",
            activity=f"{name} session",
        )
    await _drive_local_sink_loop(
        CodexDistillLocalSinkWorker(
            other_journal, global_route=other_route, owner_token="sweep"
        ),
        other_journal,
        until=lambda: all(
            job.local_sink_status is DistillLocalSinkStatus.DONE
            for job in other_journal.list_jobs()
        ),
    )
    assert "new session" in (other_route.state_dir / "resume.md").read_text()


def _set_mtime(path: Path, iso: str) -> None:
    stamp = datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    os.utime(path, (stamp, stamp))


@pytest.mark.anyio
async def test_global_sink_orders_unstamped_resume_by_file_mtime(
    tmp_path: Path,
) -> None:
    """#2301 L3: a resume with no stamped capture (hook-path CLI resume, or a
    pre-#2297 audience header) is replaced only by a strictly newer capture —
    compared against its file mtime, both host-clock times."""
    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    old = await _global_job(
        journal,
        thread_id="thread-old",
        captured_at="2026-09-14T04:00:00Z",
        distilled_at="2099-01-01T00:00:00Z",
        activity="month-old backlog",
    )
    newer = await _global_job(
        journal,
        thread_id="thread-new",
        captured_at="2026-10-14T05:00:00Z",
        distilled_at="2020-01-01T00:00:00Z",
        activity="newer bridge session",
    )
    newest = await _global_job(
        journal,
        thread_id="thread-newest",
        captured_at="2026-10-14T07:00:00Z",
        distilled_at="2026-10-14T07:01:00Z",
        activity="newest bridge session",
    )
    route = _global_route(tmp_path)
    route.state_dir.mkdir(parents=True, mode=0o700)
    resume = route.state_dir / "resume.md"
    worker = CodexDistillLocalSinkWorker(
        journal, global_route=route, owner_token="global-worker"
    )

    # Today's CLI resume (header-less) survives a month-old replayed capture…
    cli = "- 마지막 작업: hook-path CLI session today\n"
    resume.write_text(cli)
    resume.chmod(0o600)
    _set_mtime(resume, "2026-10-14T04:30:00Z")
    result = await worker.write_once(job_id=old.job_id)
    assert result.local_sink_status is DistillLocalSinkStatus.DONE
    assert resume.read_text() == cli
    # …and every job's facts are still merged.
    assert "Fact from month-old backlog." in (
        route.state_dir / "memory-facts.jsonl"
    ).read_text()

    # A capture strictly newer than the file replaces it.
    await worker.write_once(job_id=newer.job_id)
    assert "newer bridge session" in resume.read_text()

    # A bridge header without captured_at (audience-style) follows the same rule.
    lines = resume.read_text().splitlines()
    header = re.sub(r" ?captured_at=\S+", "", lines[0]).replace("distill  ", "distill ")
    resume.write_text("\n".join([header, "- 마지막 작업: uncaptured"]) + "\n")
    _set_mtime(resume, "2026-10-14T06:00:00Z")
    await worker.write_once(job_id=newest.job_id)
    assert "newest bridge session" in resume.read_text()


def test_unstamped_resume_without_new_capture_is_overwritten(tmp_path: Path) -> None:
    """No bridge capture time means nothing to order by: unchanged behavior."""
    from telegram_bot.memory.distill_local_sink import CodexLocalMemorySink
    from test_distill_local_sink import extraction_output

    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    resume = state / "resume.md"
    resume.write_text("- 마지막 작업: cli\n")
    resume.chmod(0o600)
    sink = CodexLocalMemorySink(state, audience="private", resume_never_regresses=True)
    result = sink.write(extraction_output(), job_id="9" * 64, captured_at=None)
    assert result.resume_written
    assert "cli" not in resume.read_text()


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
