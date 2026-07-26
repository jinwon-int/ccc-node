"""Read-only collector that turns distill snapshots into skill candidates (#667).

Reuses the distill journal's transport WITHOUT touching its lifecycle: it only
reads a job's already-captured ``CodexTranscriptSnapshot`` (present once the job
reaches ``SNAPSHOT_DONE``) and stages skill candidates through the idempotent
``SkillCandidateSink``. It never claims, advances, or mutates a distill job, so
the memory-distill pipeline is unaffected whether or not this collector runs.

Codex nodes compose this worker by default. A node can opt out with
``CCC_CODEX_SKILL_COLLECTOR=false``; Claude nodes never compose it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging
import re
import time
from typing import Any, Protocol

from .distill_extraction import DistillProvenance
from .skill_candidate import (
    SkillCandidateBackend,
    SkillCandidateSink,
    SkillCandidateStageResult,
)
from .skill_candidate_backend import (
    MAX_SKILL_CANDIDATE_OUTPUT_BYTES,
    SKILL_CANDIDATE_PROMPT,
    canonical_skill_candidate_input_bytes,
)
from .skill_candidate_inventory import MAX_INVENTORY_JSON_BYTES

logger = logging.getLogger(__name__)

_RESERVED_OVERHEAD_TOKENS = 8192
_RETRY_BASE_SECONDS = 5 * 60.0
_RETRY_MAX_SECONDS = 24 * 60 * 60.0
_SAFE_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class _ReservationLike(Protocol):
    @property
    def allowed(self) -> bool: ...

    def reason(self) -> str: ...


class _AutonomousSpendGate(Protocol):
    def reserve_autonomous_spend(
        self,
        provider: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        requests: int = 0,
    ) -> _ReservationLike: ...

    def refund_reservation(self, reservation: object) -> None: ...


def _body_free_error_code(error: Exception) -> str:
    code = getattr(error, "code", None)
    if isinstance(code, str) and _SAFE_ERROR_CODE_RE.fullmatch(code):
        return code
    return "skill_candidate_worker_failed"


class SkillCandidateCollectorWorker:
    """Drive one distill snapshot through the skill backend into the sink."""

    def __init__(
        self,
        *,
        journal: Any,
        backend: SkillCandidateBackend,
        sink: SkillCandidateSink,
        usage_meter: _AutonomousSpendGate | None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._journal = journal
        self._backend = backend
        self._sink = sink
        self._usage_meter = usage_meter
        self._clock = clock

    def should_collect(self, *, job_id: str) -> bool:
        """Cheap durable preflight used by the sweep before consuming its cap."""

        return not self._sink.has(job_id) and self._sink.retry_ready(
            job_id, now=self._clock()
        )

    def _record_failure(self, job_id: str, error: Exception) -> None:
        self._record_failure_code(job_id, _body_free_error_code(error))

    def _record_failure_code(self, job_id: str, error_code: str) -> None:
        self._sink.record_retry_failure(
            job_id,
            error_code=error_code,
            now=self._clock(),
            base_delay_seconds=_RETRY_BASE_SECONDS,
            max_delay_seconds=_RETRY_MAX_SECONDS,
        )

    def _refund_unused(self, reservation: _ReservationLike | None) -> None:
        if (
            reservation is not None
            and reservation.allowed
            and self._usage_meter is not None
        ):
            self._usage_meter.refund_reservation(reservation)

    async def collect_once(self, *, job_id: str) -> SkillCandidateStageResult | None:
        """Stage candidates for one job. No-op (returns None) when not ready or
        already staged. Never raises for expected skips; unexpected backend/sink
        errors propagate so the sweep loop can log and continue."""

        job = await asyncio.to_thread(self._journal.get, job_id)
        snapshot = getattr(job, "snapshot", None)
        if snapshot is None or getattr(job, "provider", None) != "codex":
            return None
        # A non-blocking per-job lease closes the preflight/provider TOCTOU
        # across bridge processes. Contenders defer instead of waiting and
        # replaying the same paid provider call after the owner finishes.
        with self._sink.claim(job.job_id) as claimed:
            if not claimed:
                return None
            # Re-check marker/backoff only after acquiring ownership.
            if not await asyncio.to_thread(
                self.should_collect, job_id=job.job_id
            ):
                return None
            provenance = DistillProvenance.model_validate(
                {
                    "provider": "codex",
                    "source_thread_hash": job.thread_hash,
                    "trigger": job.trigger,
                    "distilled_at": job.updated_at,
                }
            )
            reservation: _ReservationLike | None = None
            provider_started = False
            try:
                if self._usage_meter is not None:
                    payload = canonical_skill_candidate_input_bytes(
                        snapshot, provenance
                    )
                    # Worst-case pre-spend reservation: the exact serialized
                    # input plus bounded prompt/schema overhead and the
                    # backend's hard output cap. With a configured Codex budget
                    # this is an atomic gate; with budget=0 it still records
                    # body-free autonomous use.
                    reservation = self._usage_meter.reserve_autonomous_spend(
                        "codex",
                        input_tokens=(
                            _RESERVED_OVERHEAD_TOKENS
                            + len(SKILL_CANDIDATE_PROMPT.encode("utf-8"))
                            + len(payload)
                            + MAX_INVENTORY_JSON_BYTES
                        ),
                        output_tokens=MAX_SKILL_CANDIDATE_OUTPUT_BYTES,
                        requests=1,
                    )
                    if not reservation.allowed:
                        logger.warning(
                            "Skill-candidate collection deferred by usage budget: %s",
                            reservation.reason(),
                        )
                        return None
                    # A second marker read protects against legacy writers that
                    # do not participate in the new claim protocol. It stays
                    # asynchronous so cancellation at this pre-provider seam
                    # is explicit and refunds the unused reservation below.
                    if await asyncio.to_thread(self._sink.has, job.job_id):
                        self._refund_unused(reservation)
                        reservation = None
                        return None
                provider_started = True
                output = await self._backend.extract(
                    snapshot=snapshot, provenance=provenance
                )
                result = await asyncio.to_thread(
                    self._sink.write, output, job_id=job.job_id
                )
            except asyncio.CancelledError:
                if provider_started:
                    try:
                        self._record_failure_code(
                            job.job_id, "skill_candidate_cancelled"
                        )
                    except Exception:
                        logger.exception(
                            "Failed to persist skill-candidate cancellation backoff"
                        )
                else:
                    self._refund_unused(reservation)
                raise
            except Exception as exc:
                if provider_started:
                    try:
                        self._record_failure(job.job_id, exc)
                    except Exception:
                        logger.exception(
                            "Failed to persist skill-candidate retry state"
                        )
                else:
                    self._refund_unused(reservation)
                raise
            await asyncio.to_thread(self._sink.clear_retry, job.job_id)
            return result


__all__ = ["SkillCandidateCollectorWorker"]
