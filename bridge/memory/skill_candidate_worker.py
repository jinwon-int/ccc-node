"""Read-only collector that turns distill snapshots into skill candidates (#667).

Reuses the distill journal's transport WITHOUT touching its lifecycle: it only
reads a job's already-captured ``CodexTranscriptSnapshot`` (present once the job
reaches ``SNAPSHOT_DONE``) and stages skill candidates through the idempotent
``SkillCandidateSink``. It never claims, advances, or mutates a distill job, so
the memory-distill pipeline is unaffected whether or not this collector runs.

Wired into the bridge only behind an opt-in flag (default off); activation is a
separate canary-gated step.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .distill_extraction import DistillProvenance
from .skill_candidate import (
    SkillCandidateBackend,
    SkillCandidateSink,
    SkillCandidateStageResult,
)

logger = logging.getLogger(__name__)


class SkillCandidateCollectorWorker:
    """Drive one distill snapshot through the skill backend into the sink."""

    def __init__(
        self,
        *,
        journal: Any,
        backend: SkillCandidateBackend,
        sink: SkillCandidateSink,
    ) -> None:
        self._journal = journal
        self._backend = backend
        self._sink = sink

    async def collect_once(self, *, job_id: str) -> SkillCandidateStageResult | None:
        """Stage candidates for one job. No-op (returns None) when not ready or
        already staged. Never raises for expected skips; unexpected backend/sink
        errors propagate so the sweep loop can log and continue."""

        job = await asyncio.to_thread(self._journal.get, job_id)
        snapshot = getattr(job, "snapshot", None)
        if snapshot is None or getattr(job, "provider", None) != "codex":
            return None
        # Skip the expensive backend call for jobs already staged; the sink's
        # write is idempotent regardless, this just avoids redundant work.
        if self._sink.has(job.job_id):
            return None
        provenance = DistillProvenance.model_validate(
            {
                "provider": "codex",
                "source_thread_hash": job.thread_hash,
                "trigger": job.trigger,
                "distilled_at": job.updated_at,
            }
        )
        output = await self._backend.extract(snapshot=snapshot, provenance=provenance)
        return await asyncio.to_thread(self._sink.write, output, job_id=job.job_id)


__all__ = ["SkillCandidateCollectorWorker"]
