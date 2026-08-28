"""Contract for the read-only skill-candidate collector worker (#667).

The collector reads a distill job's snapshot and stages candidates via the
idempotent sink without ever mutating the job. Hermetic: a fake journal + fake
backend, a real sink under tmp_path.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
import threading
from types import MethodType, SimpleNamespace

import pytest

from telegram_bot.core.bot_lifecycle import BotLifecycleMixin
from telegram_bot.memory.distill_types import (
    CodexTranscriptSnapshot,
    DistillTrigger,
    TranscriptMessage,
)
from telegram_bot.memory.skill_candidate import SkillCandidateOutput, SkillCandidateSink
from telegram_bot.memory.skill_candidate_worker import SkillCandidateCollectorWorker

THREAD_HASH = hashlib.sha256(b"thread-667-worker").hexdigest()
JOB_ID = "f" * 64


def _snapshot() -> CodexTranscriptSnapshot:
    text = "run the release checklist again"
    return CodexTranscriptSnapshot(
        thread_hash=THREAD_HASH,
        last_turn_id="turn-1",
        messages=(TranscriptMessage("user", text, "2026-07-23T11:00:00Z"),),
        byte_count=len(text.encode("utf-8")),
        truncated=False,
        captured_at="2026-07-23T11:00:00Z",
    )


def _job(**overrides) -> SimpleNamespace:
    base = {
        "job_id": JOB_ID,
        "provider": "codex",
        "thread_hash": THREAD_HASH,
        "trigger": DistillTrigger.CHECKPOINT,
        "updated_at": "2026-07-23T11:00:05Z",
        "snapshot": _snapshot(),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class _FakeJournal:
    def __init__(self, job: SimpleNamespace) -> None:
        self._job = job

    def get(self, job_id: str) -> SimpleNamespace:
        assert job_id == self._job.job_id
        return self._job


def _output() -> SkillCandidateOutput:
    skill_md = (
        "---\nname: codex-release-check\n"
        "description: Capture the recurring Codex release verification checklist procedure.\n"
        "---\n\n# codex-release-check\n\n## Procedure\n1. Step.\n2. Verify.\n3. Record.\n4. Confirm.\n5. Done.\n"
    )
    return SkillCandidateOutput.model_validate(
        {
            "schema_version": 1,
            "provenance": {
                "provider": "codex",
                "source_thread_hash": THREAD_HASH,
                "trigger": "checkpoint",
                "distilled_at": "2026-07-23T11:00:05Z",
            },
            "candidates": [
                {
                    "name": "codex-release-check",
                    "summary": "Capture the recurring Codex release verification checklist procedure.",
                    "reason": "The session repeated the same release verification steps.",
                    "evidence_excerpt": "release checklist",
                    "skill_md": skill_md,
                }
            ],
        }
    )


def _piri_output() -> SkillCandidateOutput:
    skill_md = (
        "---\nname: piri-release-check\n"
        "description: Capture the recurring Piri release verification checklist procedure.\n"
        "---\n\n# piri-release-check\n\n## Procedure\n1. Step.\n2. Verify.\n3. Record.\n4. Confirm.\n5. Done.\n"
    )
    return SkillCandidateOutput.model_validate(
        {
            "schema_version": 1,
            "provenance": {
                "provider": "piri",
                "source_thread_hash": THREAD_HASH,
                "trigger": "checkpoint",
                "distilled_at": "2026-07-23T11:00:05Z",
            },
            "candidates": [
                {
                    "name": "piri-release-check",
                    "summary": "Capture the recurring Piri release verification checklist procedure.",
                    "reason": "The session repeated the same release verification steps.",
                    "evidence_excerpt": "release checklist",
                    "skill_md": skill_md,
                }
            ],
        }
    )


class _FakeBackend:
    def __init__(self, output: SkillCandidateOutput) -> None:
        self._output = output
        self.calls = 0
        self.seen_provenance = None

    async def extract(self, *, snapshot, provenance) -> SkillCandidateOutput:  # noqa: ARG002
        self.calls += 1
        self.seen_provenance = provenance
        return self._output


class _FailingBackend:
    def __init__(self) -> None:
        self.calls = 0

    async def extract(self, *, snapshot, provenance):  # noqa: ARG002
        self.calls += 1
        error = RuntimeError("body must not enter retry state")
        error.code = "skill_candidate_nonzero_exit"  # type: ignore[attr-defined]
        raise error


class _Reservation:
    def __init__(self, allowed: bool) -> None:
        self.allowed = allowed

    def reason(self) -> str:
        return "blocked-test"


class _Meter:
    def __init__(self, *, allowed: bool = True) -> None:
        self.allowed = allowed
        self.reservations = []
        self.refunds = []

    def reserve_autonomous_spend(self, provider: str, **amounts):
        self.reservations.append((provider, amounts))
        return _Reservation(self.allowed)

    def refund_reservation(self, reservation) -> None:
        self.refunds.append(reservation)


def _worker(
    tmp_path: Path,
    journal,
    backend,
    *,
    usage_meter=None,
    clock=lambda: 1_000.0,
    sink=None,
    provider="codex",
) -> SkillCandidateCollectorWorker:
    if sink is None:
        sink = SkillCandidateSink(
            tmp_path / "skill-candidates",
            tmp_path / "state" / "pending-skills",
        )
    return SkillCandidateCollectorWorker(
        journal=journal,
        backend=backend,
        sink=sink,
        usage_meter=usage_meter,
        provider=provider,
        clock=clock,
    )


def test_collect_stages_from_a_snapshot_job(tmp_path: Path) -> None:
    backend = _FakeBackend(_output())
    worker = _worker(tmp_path, _FakeJournal(_job()), backend)
    result = asyncio.run(worker.collect_once(job_id=JOB_ID))
    assert result is not None and result.candidates_staged == 1
    assert backend.calls == 1
    # Provenance is derived from the job, echoing its trigger/thread hash.
    assert backend.seen_provenance.source_thread_hash == THREAD_HASH
    assert backend.seen_provenance.trigger == DistillTrigger.CHECKPOINT
    drafts = list((tmp_path / "state" / "pending-skills").iterdir())
    assert len(drafts) == 1


def test_collect_skips_already_staged_job_without_calling_backend(tmp_path: Path) -> None:
    backend = _FakeBackend(_output())
    worker = _worker(tmp_path, _FakeJournal(_job()), backend)
    asyncio.run(worker.collect_once(job_id=JOB_ID))
    asyncio.run(worker.collect_once(job_id=JOB_ID))
    # Second sweep must not re-invoke the expensive backend.
    assert backend.calls == 1


def test_collect_skips_job_without_snapshot(tmp_path: Path) -> None:
    backend = _FakeBackend(_output())
    worker = _worker(tmp_path, _FakeJournal(_job(snapshot=None)), backend)
    assert asyncio.run(worker.collect_once(job_id=JOB_ID)) is None
    assert backend.calls == 0


def test_collect_skips_non_codex_job(tmp_path: Path) -> None:
    backend = _FakeBackend(_output())
    worker = _worker(tmp_path, _FakeJournal(_job(provider="claude")), backend)
    assert asyncio.run(worker.collect_once(job_id=JOB_ID)) is None
    assert backend.calls == 0


def test_collect_accepts_piri_job_when_provider_piri(tmp_path: Path) -> None:
    backend = _FakeBackend(_piri_output())
    worker = _worker(
        tmp_path,
        _FakeJournal(_job(provider="piri")),
        backend,
        provider="piri",
    )
    result = asyncio.run(worker.collect_once(job_id=JOB_ID))
    assert result is not None and result.candidates_staged == 1
    assert backend.calls == 1
    assert backend.seen_provenance.provider == "piri"
    drafts = list((tmp_path / "state" / "pending-skills").iterdir())
    assert len(drafts) == 1


def test_default_worker_skips_piri_job(tmp_path: Path) -> None:
    backend = _FakeBackend(_piri_output())
    worker = _worker(tmp_path, _FakeJournal(_job(provider="piri")), backend)
    assert asyncio.run(worker.collect_once(job_id=JOB_ID)) is None
    assert backend.calls == 0


def test_piri_worker_skips_codex_job(tmp_path: Path) -> None:
    backend = _FakeBackend(_output())
    worker = _worker(
        tmp_path,
        _FakeJournal(_job(provider="codex")),
        backend,
        provider="piri",
    )
    assert asyncio.run(worker.collect_once(job_id=JOB_ID)) is None
    assert backend.calls == 0


def test_worker_rejects_unsupported_provider(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _worker(
            tmp_path,
            _FakeJournal(_job()),
            _FakeBackend(_output()),
            provider="claude",
        )


def test_collect_reserves_body_free_autonomous_usage(tmp_path: Path) -> None:
    backend = _FakeBackend(_output())
    meter = _Meter()
    worker = _worker(
        tmp_path,
        _FakeJournal(_job()),
        backend,
        usage_meter=meter,
    )
    result = asyncio.run(worker.collect_once(job_id=JOB_ID))
    assert result is not None and result.candidates_staged == 1
    assert backend.calls == 1
    assert len(meter.reservations) == 1
    provider, amounts = meter.reservations[0]
    assert provider == "codex"
    assert amounts["requests"] == 1
    assert amounts["input_tokens"] > _snapshot().byte_count
    assert amounts["output_tokens"] == 64 * 1024
    assert meter.refunds == []


def test_budget_block_defers_without_backend_call(tmp_path: Path) -> None:
    backend = _FakeBackend(_output())
    meter = _Meter(allowed=False)
    worker = _worker(
        tmp_path,
        _FakeJournal(_job()),
        backend,
        usage_meter=meter,
    )
    assert asyncio.run(worker.collect_once(job_id=JOB_ID)) is None
    assert backend.calls == 0
    assert len(meter.reservations) == 1
    assert worker.should_collect(job_id=JOB_ID) is True


def test_concurrent_collectors_make_one_provider_call(tmp_path: Path) -> None:
    class _BlockingBackend:
        def __init__(self) -> None:
            self.calls = 0
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def extract(self, *, snapshot, provenance):  # noqa: ARG002
            self.calls += 1
            self.started.set()
            await self.release.wait()
            return _output()

    async def scenario() -> None:
        backend = _BlockingBackend()
        first = _worker(tmp_path, _FakeJournal(_job()), backend)
        # A distinct sink instance exercises the interprocess flock path
        # rather than relying on one worker's in-memory lock.
        second = _worker(tmp_path, _FakeJournal(_job()), backend)
        owner = asyncio.create_task(first.collect_once(job_id=JOB_ID))
        await asyncio.wait_for(backend.started.wait(), timeout=1.0)
        contender = await second.collect_once(job_id=JOB_ID)
        assert contender is None
        assert backend.calls == 1
        backend.release.set()
        staged = await asyncio.wait_for(owner, timeout=1.0)
        assert staged is not None and staged.candidates_staged == 1

    asyncio.run(scenario())


def test_pre_provider_cancellation_refunds_unused_reservation(
    tmp_path: Path,
) -> None:
    class _BlockingSecondHasSink(SkillCandidateSink):
        def __init__(self) -> None:
            super().__init__(
                tmp_path / "skill-candidates",
                tmp_path / "state" / "pending-skills",
            )
            self.calls = 0
            self.entered = threading.Event()
            self.release = threading.Event()

        def has(self, job_id: str) -> bool:
            self.calls += 1
            if self.calls == 2:
                self.entered.set()
                self.release.wait(timeout=2.0)
            return super().has(job_id)

    async def scenario() -> None:
        sink = _BlockingSecondHasSink()
        meter = _Meter()
        backend = _FakeBackend(_output())
        worker = _worker(
            tmp_path,
            _FakeJournal(_job()),
            backend,
            usage_meter=meter,
            sink=sink,
        )
        task = asyncio.create_task(worker.collect_once(job_id=JOB_ID))
        entered = await asyncio.to_thread(sink.entered.wait, 1.0)
        assert entered
        task.cancel()
        result = await asyncio.gather(task, return_exceptions=True)
        sink.release.set()
        assert isinstance(result[0], asyncio.CancelledError)
        assert backend.calls == 0
        assert len(meter.refunds) == 1

    asyncio.run(scenario())


def test_provider_cancellation_is_charged_and_backed_off(tmp_path: Path) -> None:
    class _CancelableBackend:
        def __init__(self) -> None:
            self.calls = 0
            self.started = asyncio.Event()

        async def extract(self, *, snapshot, provenance):  # noqa: ARG002
            self.calls += 1
            self.started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    async def scenario() -> None:
        meter = _Meter()
        backend = _CancelableBackend()
        worker = _worker(
            tmp_path,
            _FakeJournal(_job()),
            backend,
            usage_meter=meter,
        )
        task = asyncio.create_task(worker.collect_once(job_id=JOB_ID))
        await asyncio.wait_for(backend.started.wait(), timeout=1.0)
        task.cancel()
        result = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(result[0], asyncio.CancelledError)
        assert backend.calls == 1
        assert meter.refunds == []
        assert worker.should_collect(job_id=JOB_ID) is False
        retry_path = (
            tmp_path
            / "skill-candidates"
            / ".retries"
            / f"{JOB_ID}.json"
        )
        assert '"error_code":"skill_candidate_cancelled"' in retry_path.read_text()

    asyncio.run(scenario())


def test_pre_provider_local_failure_refunds_unused_reservation(
    tmp_path: Path,
) -> None:
    class _FailingSecondHasSink(SkillCandidateSink):
        def __init__(self) -> None:
            super().__init__(
                tmp_path / "skill-candidates",
                tmp_path / "state" / "pending-skills",
            )
            self.calls = 0

        def has(self, job_id: str) -> bool:
            self.calls += 1
            if self.calls == 2:
                raise OSError("local marker read failed")
            return super().has(job_id)

    sink = _FailingSecondHasSink()
    meter = _Meter()
    backend = _FakeBackend(_output())
    worker = _worker(
        tmp_path,
        _FakeJournal(_job()),
        backend,
        usage_meter=meter,
        sink=sink,
    )

    with pytest.raises(OSError, match="marker read failed"):
        asyncio.run(worker.collect_once(job_id=JOB_ID))
    assert backend.calls == 0
    assert len(meter.refunds) == 1
    retry_dir = tmp_path / "skill-candidates" / ".retries"
    assert not retry_dir.exists() or not any(retry_dir.iterdir())


def test_claim_directory_symlink_fails_closed(tmp_path: Path) -> None:
    queue = tmp_path / "skill-candidates"
    outside = tmp_path / "outside"
    queue.mkdir(mode=0o700)
    outside.mkdir(mode=0o700)
    (queue / ".claims").symlink_to(outside, target_is_directory=True)
    sink = SkillCandidateSink(queue, tmp_path / "state" / "pending-skills")

    with pytest.raises((PermissionError, ValueError)):
        with sink.claim(JOB_ID):
            pytest.fail("unsafe claim path must not be entered")


def test_backend_failure_gets_durable_exponential_backoff(tmp_path: Path) -> None:
    now = [1_000.0]
    backend = _FailingBackend()
    worker = _worker(
        tmp_path,
        _FakeJournal(_job()),
        backend,
        clock=lambda: now[0],
    )

    with pytest.raises(RuntimeError):
        asyncio.run(worker.collect_once(job_id=JOB_ID))
    assert backend.calls == 1
    assert worker.should_collect(job_id=JOB_ID) is False

    # A new worker (simulating a process restart) observes the same durable
    # retry state and does not immediately call the provider again.
    restarted = _worker(
        tmp_path,
        _FakeJournal(_job()),
        backend,
        clock=lambda: now[0],
    )
    assert asyncio.run(restarted.collect_once(job_id=JOB_ID)) is None
    assert backend.calls == 1

    now[0] += 5 * 60
    assert restarted.should_collect(job_id=JOB_ID) is True
    with pytest.raises(RuntimeError):
        asyncio.run(restarted.collect_once(job_id=JOB_ID))
    assert backend.calls == 2

    retry_path = tmp_path / "skill-candidates" / ".retries" / f"{JOB_ID}.json"
    assert retry_path.exists()
    assert (retry_path.stat().st_mode & 0o777) == 0o600


def test_retry_directory_symlink_fails_closed_before_provider_call(
    tmp_path: Path,
) -> None:
    queue = tmp_path / "skill-candidates"
    outside = tmp_path / "outside"
    queue.mkdir(mode=0o700)
    outside.mkdir(mode=0o700)
    (queue / ".retries").symlink_to(outside, target_is_directory=True)
    backend = _FakeBackend(_output())
    worker = _worker(tmp_path, _FakeJournal(_job()), backend)

    with pytest.raises((PermissionError, ValueError)):
        worker.should_collect(job_id=JOB_ID)
    assert backend.calls == 0


def test_collector_loop_hard_bounds_provider_attempts_per_sweep() -> None:
    stop_event = asyncio.Event()

    class _Journal:
        calls = 0

        def list_jobs(self):
            self.calls += 1
            if self.calls > 1:
                stop_event.set()
            return tuple(
                SimpleNamespace(
                    job_id=f"{index:x}" * 64,
                    provider="codex",
                    snapshot=object(),
                )
                for index in range(1, 6)
            )

    class _Worker:
        def __init__(self) -> None:
            self.calls = []

        def should_collect(self, *, job_id: str) -> bool:
            return True

        async def collect_once(self, *, job_id: str):
            self.calls.append(job_id)

    worker = _Worker()
    lifecycle = SimpleNamespace(
        _skill_candidate_collector_worker=worker,
        _distill_journal=_Journal(),
        _config=SimpleNamespace(
            distill_extraction_poll_interval=0.001,
            codex_skill_collector_max_jobs_per_sweep=2,
        ),
    )
    # The loop scans through the mixin's shared journal sweep; bind the real
    # helper so this duck-typed lifecycle exercises the production path.
    lifecycle._distill_sweep_jobs = MethodType(
        BotLifecycleMixin._distill_sweep_jobs, lifecycle
    )
    asyncio.run(
        BotLifecycleMixin._skill_candidate_collector_loop(lifecycle, stop_event)
    )
    assert len(worker.calls) == 2
