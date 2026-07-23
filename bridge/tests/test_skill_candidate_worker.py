"""Contract for the read-only skill-candidate collector worker (#667).

The collector reads a distill job's snapshot and stages candidates via the
idempotent sink without ever mutating the job. Hermetic: a fake journal + fake
backend, a real sink under tmp_path.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace

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


class _FakeBackend:
    def __init__(self, output: SkillCandidateOutput) -> None:
        self._output = output
        self.calls = 0
        self.seen_provenance = None

    async def extract(self, *, snapshot, provenance) -> SkillCandidateOutput:  # noqa: ARG002
        self.calls += 1
        self.seen_provenance = provenance
        return self._output


def _worker(tmp_path: Path, journal, backend) -> SkillCandidateCollectorWorker:
    sink = SkillCandidateSink(tmp_path / "skill-candidates", tmp_path / "state" / "pending-skills")
    return SkillCandidateCollectorWorker(journal=journal, backend=backend, sink=sink)


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
