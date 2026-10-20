"""Journal contract for independently replayable Codex local-sink work (#465)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import re

import pytest

import test_distill_worker as fixtures

from telegram_bot.memory.distill_journal import (
    LOCAL_SINK_SUPERSEDED_ROUTE,
    DistillJournal,
    is_local_sink_superseded_route,
)
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
    # #2333: routed work is closed as superseded, still never adopted.
    assert is_local_sink_superseded_route(routed_journal.get(routed.job_id))


async def _supersede(journal: DistillJournal, job_id: str, **kwargs) -> DistillJob:
    assert (
        journal.claim_local_sink(
            job_id,
            owner_token="global",
            audience_routes=False,
            global_route=True,
            **kwargs,
        )
        is None
    )
    return journal.get(job_id)


@pytest.mark.anyio
async def test_global_route_supersedes_routed_pending_without_adopting(
    tmp_path: Path,
) -> None:
    """#2333: off/curated closes audience-routed PENDING work, never adopts it."""

    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    routed = await extracted_job(journal)
    assert routed.local_sink_status is DistillLocalSinkStatus.PENDING
    before = journal.get(routed.job_id).to_dict()

    superseded = await _supersede(journal, routed.job_id, now=fixtures.utc(5))

    # Rollback-safe representation: a pre-existing terminal status plus a
    # marker error_code, never a new status value.
    assert superseded.local_sink_status is DistillLocalSinkStatus.TERMINAL_FAILED
    assert superseded.error_code == LOCAL_SINK_SUPERSEDED_ROUTE
    assert is_local_sink_superseded_route(superseded)
    after = superseded.to_dict()
    # Only status, marker and timestamp move; output, route, counters stay.
    changed = {key for key in before.keys() | after.keys() if before.get(key) != after.get(key)}
    assert changed == {"local_sink_status", "error_code", "updated_at"}
    assert superseded.local_sink_attempts == 0
    assert superseded.owner_token is None and superseded.lease_expires_at is None
    reopened = DistillJournal(journal.root)
    reopened.initialize()
    assert reopened.get(routed.job_id) == superseded

    # Idempotent: further global claims neither claim nor rewrite it.
    record = journal.job_path(routed.job_id).read_bytes()
    await _supersede(journal, routed.job_id, now=fixtures.utc(9))
    assert journal.job_path(routed.job_id).read_bytes() == record


@pytest.mark.anyio
async def test_global_route_supersedes_routed_retryable_failed_work(
    tmp_path: Path,
) -> None:
    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    routed = await extracted_job(journal)
    claimed = journal.claim_local_sink(routed.job_id, owner_token="aud")
    assert claimed is not None
    failed = journal.mark_local_sink_retryable_failed(
        routed.job_id,
        owner_token="aud",
        lease_epoch=claimed.local_sink_lease_epoch,
        error_code="local_sink_io_failed",
    )

    superseded = await _supersede(journal, routed.job_id)
    assert is_local_sink_superseded_route(superseded)
    assert superseded.local_sink_attempts == failed.local_sink_attempts == 1


@pytest.mark.anyio
async def test_routed_pending_without_any_route_stays_pending_untouched(
    tmp_path: Path,
) -> None:
    """#2333: global_route=False keeps the historical None / PENDING contract."""

    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    routed = await extracted_job(journal)
    record = journal.job_path(routed.job_id).read_bytes()

    assert (
        journal.claim_local_sink(
            routed.job_id, owner_token="none", audience_routes=False, global_route=False
        )
        is None
    )
    assert journal.job_path(routed.job_id).read_bytes() == record
    assert journal.get(routed.job_id).local_sink_status is DistillLocalSinkStatus.PENDING


@pytest.mark.anyio
async def test_audience_routes_reclaim_superseded_work_like_pending(
    tmp_path: Path,
) -> None:
    """#2333: re-enabling audience mode resumes superseded work exactly once."""

    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    routed = await extracted_job(journal)
    await _supersede(journal, routed.job_id)

    claimed = journal.claim_local_sink(routed.job_id, owner_token="aud")
    assert claimed is not None
    assert claimed.local_sink_status is DistillLocalSinkStatus.RUNNING
    # Superseding spent no attempt; the counter is preserved, not reset.
    assert claimed.local_sink_attempts == 1
    assert claimed.error_code is None
    assert (claimed.memory_audience, claimed.memory_scope) == ("private", PRIVATE_SCOPE)
    done = journal.mark_local_sink_done(
        routed.job_id,
        owner_token="aud",
        lease_epoch=claimed.local_sink_lease_epoch,
    )
    assert done.local_sink_status is DistillLocalSinkStatus.DONE
    assert journal.claim_local_sink(routed.job_id, owner_token="aud") is None


@pytest.mark.anyio
async def test_genuine_terminal_failures_are_never_resumed_as_superseded(
    tmp_path: Path,
) -> None:
    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    routed = await extracted_job(journal)
    claimed = journal.claim_local_sink(routed.job_id, owner_token="aud")
    assert claimed is not None
    failed = journal.mark_local_sink_terminal_failed(
        routed.job_id,
        owner_token="aud",
        lease_epoch=claimed.local_sink_lease_epoch,
        error_code="local_sink_output_invalid",
    )
    assert not is_local_sink_superseded_route(failed)
    assert journal.claim_local_sink(routed.job_id, owner_token="aud") is None
    # A global sink does not touch terminal work either.
    assert journal.get(routed.job_id) == (await _supersede(journal, routed.job_id))
    assert journal.get(routed.job_id).error_code == "local_sink_output_invalid"


@pytest.mark.anyio
async def test_wiki_stage_never_erases_the_superseded_marker(tmp_path: Path) -> None:
    """The Wiki stage shares error_code; resumability must survive it."""

    from datetime import timedelta

    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    routed = await extracted_job(journal, wiki_enabled=True)
    await _supersede(journal, routed.job_id)

    wiki = journal.claim_wiki_sink(routed.job_id, owner_token="wiki", now=fixtures.utc(10))
    assert wiki is not None and is_local_sink_superseded_route(wiki)
    failed = journal.mark_wiki_sink_retryable_failed(
        routed.job_id,
        owner_token="wiki",
        lease_epoch=wiki.wiki_sink_lease_epoch,
        error_code="wiki_sink_io_failed",
    )
    assert is_local_sink_superseded_route(failed)
    wiki = journal.claim_wiki_sink(routed.job_id, owner_token="wiki", now=fixtures.utc(20))
    assert wiki is not None
    # Lease expiry recovery keeps the marker too.
    journal.recover_stale_running(now=fixtures.utc(20) + timedelta(hours=1))
    recovered = journal.get(routed.job_id)
    assert is_local_sink_superseded_route(recovered)
    wiki = journal.claim_wiki_sink(routed.job_id, owner_token="wiki")
    assert wiki is not None
    done = journal.mark_wiki_sink_done(
        routed.job_id, owner_token="wiki", lease_epoch=wiki.wiki_sink_lease_epoch
    )
    assert is_local_sink_superseded_route(done)
    assert journal.claim_local_sink(routed.job_id, owner_token="aud") is not None


@pytest.mark.anyio
async def test_unrouted_unroutable_adoption_is_unchanged_by_superseding(
    tmp_path: Path,
) -> None:
    """#2296 adoption of route-free orphans still runs beside #2333."""

    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    orphan = await extracted_job(journal, memory_audience=None, memory_scope=None)
    claimed = journal.claim_local_sink(
        orphan.job_id, owner_token="global", audience_routes=False, global_route=True
    )
    assert claimed is not None
    assert claimed.local_sink_status is DistillLocalSinkStatus.RUNNING
    done = journal.mark_local_sink_done(
        orphan.job_id,
        owner_token="global",
        lease_epoch=claimed.local_sink_lease_epoch,
    )
    assert done.local_sink_status is DistillLocalSinkStatus.DONE
    assert not is_local_sink_superseded_route(done)


# Statuses a pre-#2333 bridge parses; a rollback target must read every
# record the new code writes.
_PRE_2333_LOCAL_SINK_STATUSES = {
    "pending",
    "running",
    "retryable_failed",
    "done",
    "terminal_failed",
    "unroutable",
}
_PRE_2333_BASE_REF = "22d24780"


def _load_pre_2333_journal_module(tmp_path: Path):
    import importlib
    import subprocess
    import sys

    repo = Path(__file__).resolve().parents[2]
    package = tmp_path / "base-src" / "pre_2333_memory"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    for name in ("distill_types", "distill_extraction", "journal_core", "distill_journal"):
        try:
            source = subprocess.run(
                ["git", "-C", str(repo), "show", f"{_PRE_2333_BASE_REF}:bridge/memory/{name}.py"],
                capture_output=True,
                check=True,
                text=True,
            ).stdout
        except (OSError, subprocess.CalledProcessError):
            pytest.skip("pre-#2333 bridge sources unavailable in this checkout")
        (package / f"{name}.py").write_text(source)
    sys.path.insert(0, str(package.parent))
    try:
        return importlib.import_module("pre_2333_memory.distill_journal")
    finally:
        sys.path.remove(str(package.parent))
        for loaded in [key for key in sys.modules if key.startswith("pre_2333_memory")]:
            sys.modules.pop(loaded, None)


@pytest.mark.anyio
async def test_superseded_record_stays_readable_after_a_rollback(
    tmp_path: Path,
) -> None:
    """#2333: an older bridge must parse what the new code writes.

    Old list_jobs()/recover_stale_running() raise on an unparseable record,
    which would stall the whole local-sink loop after a self-update rollback.
    """

    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    routed = await extracted_job(journal, wiki_enabled=False)
    await _supersede(journal, routed.job_id)
    record = json.loads(journal.job_path(routed.job_id).read_text())
    assert record["local_sink_status"] in _PRE_2333_LOCAL_SINK_STATUSES
    assert re.fullmatch(r"^[a-z][a-z0-9_]{0,63}$", record["error_code"])

    base = _load_pre_2333_journal_module(tmp_path)
    old = base.DistillJournal(journal.root)
    old.initialize()
    (job,) = old.list_jobs()
    assert job.local_sink_status.value == "terminal_failed"
    assert job.error_code == LOCAL_SINK_SUPERSEDED_ROUTE
    assert old.recover_stale_running() == 0
    # On the old build it is plain terminal work: never claimed, ages out.
    assert old.claim_local_sink(routed.job_id, owner_token="old") is None
    from datetime import datetime, timedelta, timezone

    later = datetime.now(timezone.utc) + timedelta(days=30)
    assert old.prune_terminal_jobs(older_than_seconds=60, now=later) == (routed.job_id,)


@pytest.mark.anyio
async def test_prune_ages_out_superseded_work_unless_audience_routes_exist(
    tmp_path: Path,
) -> None:
    """#2333: superseded work is terminal for retention like DONE/UNROUTABLE."""

    from datetime import datetime, timedelta, timezone

    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    routed = await extracted_job(journal, wiki_enabled=False)
    await _supersede(journal, routed.job_id)
    later = datetime.now(timezone.utc) + timedelta(days=30)

    assert journal.prune_terminal_jobs(
        older_than_seconds=60, now=later, keep_superseded_route=True
    ) == ()
    assert is_local_sink_superseded_route(journal.get(routed.job_id))
    # keep_unroutable (global sink) does not protect superseded audience work.
    assert journal.prune_terminal_jobs(
        older_than_seconds=60, now=later, keep_unroutable=True
    ) == (routed.job_id,)


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
        assert "keep_superseded_route" not in calls[-1]


@pytest.mark.anyio
async def test_sweep_prune_keeps_superseded_only_with_audience_routes() -> None:
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
        (SimpleNamespace(audience_routes_enabled=True), True),
        (SimpleNamespace(audience_routes_enabled=False, global_route_enabled=True), False),
        (None, False),
    ):
        bot = TelegramBot.__new__(TelegramBot)
        bot._distill_journal = _Journal()
        bot._distill_local_sink_worker = worker
        bot._config = SimpleNamespace(distill_job_retention_seconds=3600.0)
        calls.clear()
        await bot._distill_sweep_jobs(0.0)
        assert calls, "sweep did not prune"
        assert calls[-1].get("keep_superseded_route", False) is expected


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
