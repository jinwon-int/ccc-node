"""Read-only collector that turns distill snapshots into skill candidates (#667).

Reuses the distill journal's transport WITHOUT touching its lifecycle: it only
reads a job's already-captured ``CodexTranscriptSnapshot`` (present once the job
reaches ``SNAPSHOT_DONE``) and stages skill candidates through the idempotent
``SkillCandidateSink``. It never claims, advances, or mutates a distill job, so
the memory-distill pipeline is unaffected whether or not this collector runs.

Codex, Piri, and Danso nodes compose this worker (one instance per provider,
bound to that provider's journal jobs and install target). A node can opt out
with its provider-specific collector setting; Claude nodes never compose it.
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
    RETRY_QUARANTINED,
    RETRY_READY,
    SkillCandidateBackend,
    SkillCandidateSink,
    SkillCandidateStageResult,
    body_free_exit_status,
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
# Consecutive provider-started failures after which a job is quarantined: no
# further retries and therefore no further worst-case usage reservations. Every
# provider-started failure keeps its reservation (conservative accounting), so
# this cap is what bounds the cost of a persistently failing job.
MAX_SKILL_CANDIDATE_ATTEMPTS = 5


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


def _body_free_error_code(error: BaseException) -> str:
    code = getattr(error, "code", None)
    if isinstance(code, str) and _SAFE_ERROR_CODE_RE.fullmatch(code):
        return code
    return "skill_candidate_worker_failed"


def skill_candidate_failure_fields(error: BaseException) -> tuple[str, int | None]:
    """Body-free ``(error_code, exit_status)`` for logs and retry state.

    Only the stable classified code and the provider's integer exit status are
    ever exposed; exception messages, stdout, and stderr never are.
    """

    return (
        _body_free_error_code(error),
        body_free_exit_status(getattr(error, "exit_status", None)),
    )


class SkillCandidateCollectorWorker:
    """Drive one distill snapshot through the skill backend into the sink."""

    def __init__(
        self,
        *,
        journal: Any,
        backend: SkillCandidateBackend,
        sink: SkillCandidateSink,
        usage_meter: _AutonomousSpendGate | None,
        provider: str = "codex",
        clock: Callable[[], float] = time.time,
        max_attempts: int = MAX_SKILL_CANDIDATE_ATTEMPTS,
    ) -> None:
        if provider not in {"codex", "piri", "danso"}:
            raise ValueError("unsupported skill-candidate collector provider")
        if type(max_attempts) is not int or max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")
        self._max_attempts = max_attempts
        # Job ids whose quarantine was already logged by this process.
        self._quarantine_logged: set[str] = set()
        self._provider = provider
        self._journal = journal
        self._backend = backend
        self._sink = sink
        self._usage_meter = usage_meter
        self._clock = clock

    @property
    def provider(self) -> str:
        """Provider whose distill jobs this worker is configured to collect."""

        return self._provider

    def should_collect(self, *, job_id: str) -> bool:
        """Cheap durable preflight used by the sweep before consuming its cap."""

        if self._sink.has(job_id):
            return False
        status = self._sink.retry_status(
            job_id, now=self._clock(), max_attempts=self._max_attempts
        )
        if status == RETRY_QUARANTINED and job_id not in self._quarantine_logged:
            self._quarantine_logged.add(job_id)
            logger.warning(
                "Skill-candidate job quarantined; skipping without provider call "
                "or usage reservation: provider=%s job_id=%s max_attempts=%d "
                "(remove its .retries record to requeue)",
                self._provider,
                job_id,
                self._max_attempts,
            )
        return status == RETRY_READY

    def _record_failure(self, job_id: str, error: BaseException) -> None:
        error_code, exit_status = skill_candidate_failure_fields(error)
        self._record_failure_code(job_id, error_code, exit_status=exit_status)

    def _record_failure_code(
        self, job_id: str, error_code: str, *, exit_status: int | None = None
    ) -> None:
        record = self._sink.record_retry_failure(
            job_id,
            error_code=error_code,
            now=self._clock(),
            base_delay_seconds=_RETRY_BASE_SECONDS,
            max_delay_seconds=_RETRY_MAX_SECONDS,
            exit_status=exit_status,
            max_attempts=self._max_attempts,
        )
        if record.get("quarantined") is True and job_id not in self._quarantine_logged:
            self._quarantine_logged.add(job_id)
            logger.warning(
                "Skill-candidate job quarantined after %s failed provider "
                "attempts; no further retries or usage reservations: "
                "provider=%s job_id=%s code=%s exit_status=%s "
                "(remove its .retries record to requeue)",
                record.get("attempts"),
                self._provider,
                job_id,
                error_code,
                exit_status,
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
        if snapshot is None or getattr(job, "provider", None) != self._provider:
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
                    "provider": self._provider,
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
                        self._provider,
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


__all__ = [
    "MAX_SKILL_CANDIDATE_ATTEMPTS",
    "SkillCandidateCollectorWorker",
    "skill_candidate_failure_fields",
]
