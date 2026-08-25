"""Replay-safe worker for the isolated Codex distill extraction stage."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging
import math
import re
import secrets
import time
from typing import Literal, Protocol, cast

from .distill_extraction import (
    DecisionReasonMissingError,
    DistillBackend,
    DistillExtractionOutput,
    build_extraction_input,
    validate_live_decision_reasons,
)
from .codex_exec_backend import MAX_EXTRACTION_JSON_BYTES
from .distill_journal import DistillJournal
from .distill_guard import DistillGuard, PROVIDER_CIRCUIT_CODES
from .distill_types import DistillExtractionAccounting, DistillJob

logger = logging.getLogger(__name__)


class _ReservationLike(Protocol):
    @property
    def allowed(self) -> bool: ...

    def reason(self) -> str: ...


class AutonomousSpendGate(Protocol):
    """Structural view of core.usage_meter.UsageMeter used by this worker.

    Declared here as a Protocol so the memory package does not import the
    core package (keeping the bridge internal import graph acyclic).
    """

    def reserve_autonomous_spend(
        self,
        provider: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        requests: int = 0,
    ) -> _ReservationLike: ...

    def refund_reservation(self, reservation: object) -> None: ...

_SAFE_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

# Worst-case autonomous pre-spend reservation (#388). The codex exec backend
# discards provider stdout, so per-attempt token usage is not observable here
# until #465's cost-metering criterion lands. Every extraction attempt charges
# a post-serialization bound over the COMPLETE request instead: canonical
# JSON escaping expands one raw snapshot byte to at most six serialized bytes
# (backslash-u escapes), BPE tokenizers emit at most ~1 token per serialized
# byte, the flat overhead covers the extraction prompt and schema, and the
# output allowance equals the backend's hard output-size cap (output tokens
# cannot exceed its JSON bytes). Budgets must fit one maximal attempt or that
# work stays deferred by design.
_RESERVED_OVERHEAD_TOKENS = 8192
_RESERVED_TOKENS_PER_BYTE = 6
_RESERVED_OUTPUT_TOKENS = MAX_EXTRACTION_JSON_BYTES
# ``*_output_invalid`` is a property of one sampled provider answer, not of the
# job: the same snapshot re-extracted usually parses. Treating it as terminal
# discarded a whole session's memory on a single malformed response, with no
# second chance. It is retryable and therefore bounded by ``max_attempts`` and
# the usage budget like every other transient failure.
_RETRYABLE_BACKEND_CODES = frozenset(
    {
        "distill_spawn_failed",
        "distill_timeout",
        "distill_io_failed",
        "distill_nonzero_exit",
        "distill_output_invalid",
        "distill_decision_reason_missing",
        "codex_distill_spawn_failed",
        "codex_distill_timeout",
        "codex_distill_io_failed",
        "codex_distill_nonzero_exit",
        "codex_distill_output_invalid",
        "codex_distill_decision_reason_missing",
    }
    | PROVIDER_CIRCUIT_CODES
)
_TERMINAL_BACKEND_CODES = frozenset(
    {
        "distill_config_invalid",
        "distill_input_invalid",
        "distill_executable_unsafe",
        "distill_schema_unsafe",
        "distill_output_missing",
        "distill_output_unsafe",
        "distill_output_too_large",
        "codex_distill_config_invalid",
        "codex_distill_input_invalid",
        "codex_distill_schema_unsafe",
        "codex_distill_executable_unsafe",
        "codex_distill_output_missing",
        "codex_distill_output_unsafe",
        "codex_distill_output_too_large",
    }
)


def _body_free_error_code(error: Exception) -> tuple[str, bool]:
    code = getattr(error, "code", None)
    if not isinstance(code, str) or not _SAFE_ERROR_CODE_RE.fullmatch(code):
        return "distill_backend_failed", False
    if code in _TERMINAL_BACKEND_CODES:
        return code, True
    if code in _RETRYABLE_BACKEND_CODES:
        return code, False
    return "distill_backend_failed", False


class CodexDistillExtractionWorker:
    """Claim one snapshot and durably retain one validated extraction result."""

    def __init__(
        self,
        journal: DistillJournal,
        backend: DistillBackend,
        *,
        usage_meter: AutonomousSpendGate | None,
        owner_token: str | None = None,
        lease_seconds: int = 300,
        max_attempts: int = 5,
        wiki_enabled: bool = True,
        honcho_enabled: bool = True,
        extractor_provider: Literal["claude", "codex", "piri"] = "codex",
        model: str = "provider-default",
        clock: Callable[[], float] = time.monotonic,
        guard: DistillGuard | None = None,
        provider_cooldown_seconds: int = 3600,
        retry_backoff_base_seconds: int = 300,
        retry_backoff_max_seconds: int = 6 * 3600,
    ) -> None:
        if (
            lease_seconds <= 0
            or max_attempts <= 0
            or type(wiki_enabled) is not bool
            or type(honcho_enabled) is not bool
            or extractor_provider not in {"claude", "codex", "piri"}
            or type(provider_cooldown_seconds) is not int
            or provider_cooldown_seconds <= 0
            or type(retry_backoff_base_seconds) is not int
            or retry_backoff_base_seconds <= 0
            or type(retry_backoff_max_seconds) is not int
            or retry_backoff_max_seconds < retry_backoff_base_seconds
        ):
            raise ValueError("invalid distill extraction worker configuration")
        self._journal = journal
        self._backend = backend
        self._owner_token = owner_token or secrets.token_hex(16)
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts
        self._wiki_enabled = wiki_enabled
        self._honcho_enabled = honcho_enabled
        self._extractor_provider = extractor_provider
        self._usage_meter = usage_meter
        self._guard = guard
        self._provider_cooldown_seconds = provider_cooldown_seconds
        self._retry_backoff_base_seconds = retry_backoff_base_seconds
        self._retry_backoff_max_seconds = retry_backoff_max_seconds
        try:
            DistillExtractionAccounting(model, 0, 0, 0)
        except ValueError:
            raise ValueError("invalid distill extraction worker model") from None
        self._model = model
        self._clock = clock

    def _accounting(
        self,
        *,
        snapshot_bytes: int,
        started_at: float,
        estimated_max_tokens: int,
    ) -> DistillExtractionAccounting:
        elapsed = self._clock() - started_at
        duration_ms = (
            min(10**12, round(elapsed * 1000))
            if math.isfinite(elapsed) and elapsed > 0
            else 0
        )
        return DistillExtractionAccounting(
            model=self._model,
            snapshot_bytes=snapshot_bytes,
            duration_ms=duration_ms,
            estimated_max_tokens=estimated_max_tokens,
        )

    async def _fail(
        self,
        claimed: DistillJob,
        *,
        error_code: str,
        terminal: bool,
        accounting: DistillExtractionAccounting | None = None,
        retry_after_seconds: int = 0,
    ) -> DistillJob:
        if terminal:
            return await asyncio.to_thread(
                self._journal.mark_extraction_terminal_failed,
                claimed.job_id,
                owner_token=self._owner_token,
                lease_epoch=claimed.extraction_lease_epoch,
                error_code=error_code,
                accounting=accounting,
            )
        return await asyncio.to_thread(
            self._journal.mark_extraction_retryable_failed,
            claimed.job_id,
            owner_token=self._owner_token,
            lease_epoch=claimed.extraction_lease_epoch,
            error_code=error_code,
            accounting=accounting,
            retry_after_seconds=retry_after_seconds,
        )

    def _retry_delay(self, extraction_attempt: int) -> int:
        exponent = max(0, min(30, extraction_attempt - 1))
        return min(
            self._retry_backoff_max_seconds,
            self._retry_backoff_base_seconds * (2**exponent),
        )

    def _guard_decision(self):
        if self._guard is None:
            return None
        return self._guard.decision(self._extractor_provider, self._model)

    def _retry_delay_after_error(
        self,
        *,
        error_code: str,
        terminal: bool,
        extraction_attempt: int,
    ) -> int:
        if terminal:
            return 0
        retry_after_seconds = self._retry_delay(extraction_attempt)
        if error_code not in PROVIDER_CIRCUIT_CODES or self._guard is None:
            return retry_after_seconds
        try:
            circuit = self._guard.trip(
                self._extractor_provider,
                self._model,
                error_code=error_code,
                cooldown_seconds=self._provider_cooldown_seconds,
            )
            return max(
                retry_after_seconds,
                circuit.remaining_seconds(time.time()),
            )
        except Exception:
            logger.warning(
                "Distill circuit breaker state write failed: provider=%s",
                self._extractor_provider,
            )
            return retry_after_seconds

    def _refund_unused_reservation(self, reservation: _ReservationLike | None) -> None:
        """Return budget reserved for an attempt the provider never ran.

        Every pre-provider exit from :meth:`extract_once` must route through
        here. The reservation is prospective (#388): it is charged before the
        backend can run, so any path that returns before ``self._backend
        .extract`` leaves recorded autonomous spend for work that never
        happened. Fail-open by design — a refund that raises keeps the charge
        (conservative) rather than breaking the extraction path.
        """

        if self._usage_meter is None or reservation is None:
            return
        try:
            self._usage_meter.refund_reservation(reservation)
        except Exception:
            logger.exception("Usage reservation refund failed; keeping charge")

    async def extract_once(self, *, job_id: str) -> DistillJob:
        reservation: _ReservationLike | None = None
        preview = await asyncio.to_thread(self._journal.get, job_id)
        guard_decision = self._guard_decision()
        if guard_decision is not None and not guard_decision.allowed:
            logger.warning(
                "Distill extraction deferred by circuit breaker: code=%s",
                guard_decision.code,
            )
            return preview
        preview_snapshot = getattr(preview, "snapshot", None)
        snapshot_bytes = (
            preview_snapshot.byte_count if preview_snapshot is not None else 0
        )
        estimated_max_tokens = (
            _RESERVED_OVERHEAD_TOKENS
            + _RESERVED_OUTPUT_TOKENS
            + max(0, snapshot_bytes) * _RESERVED_TOKENS_PER_BYTE
        )
        if self._usage_meter is not None:
            # Prospective atomic admit-and-charge (#388): the FULL bounded
            # attempt cost — flat overhead plus the persisted snapshot's
            # size charge — is reserved in one meter step before the provider
            # can run, and admission requires the whole reservation to fit
            # under the daily cap. Recorded autonomous spend therefore never
            # crosses the cap; an attempt whose bounded cost alone exceeds
            # the cap stays deferred until the operator raises the budget.
            # A blocked decision leaves the job unclaimed, so no attempt is
            # burned and it replays once the daily window resets.
            reservation = self._usage_meter.reserve_autonomous_spend(
                self._extractor_provider,
                input_tokens=estimated_max_tokens,
                requests=1,
            )
            if not reservation.allowed:
                logger.warning(
                    "Distill extraction deferred by usage budget: %s",
                    reservation.reason(),
                )
                return preview
        claimed = await asyncio.to_thread(
            self._journal.claim_extraction,
            job_id,
            owner_token=self._owner_token,
            lease_seconds=self._lease_seconds,
            max_attempts=self._max_attempts,
        )
        if claimed is None:
            # The reserved attempt never started (already done, leased
            # elsewhere, or exhausted): return the exact reservation —
            # the handle pins its accounting day and dimensions — so
            # no-op invocations cannot drain the budget. A crash before
            # this refund leaves the charge in place — conservative.
            self._refund_unused_reservation(reservation)
            return await asyncio.to_thread(self._journal.get, job_id)
        snapshot = claimed.snapshot
        if snapshot is None or snapshot.thread_hash != claimed.thread_hash:
            # Same rule as the unclaimed path: these validations fail BEFORE
            # `self._backend.extract` runs, so the provider was never invoked
            # and the reserved budget must go back. Without this the daily
            # autonomous cap is consumed by work that never happened, and a
            # persistently malformed job drains the budget once per tick.
            self._refund_unused_reservation(reservation)
            return await self._fail(
                claimed,
                error_code="snapshot_thread_mismatch",
                terminal=True,
            )
        try:
            extraction_input = build_extraction_input(
                snapshot,
                trigger=claimed.trigger,
                provider=cast(
                    Literal["claude", "codex", "piri"], claimed.provider
                ),
            )
        except (TypeError, ValueError):
            self._refund_unused_reservation(reservation)
            return await self._fail(
                claimed,
                error_code="distill_input_invalid",
                terminal=True,
            )
        guard_decision = self._guard_decision()
        if guard_decision is not None and not guard_decision.allowed:
            self._refund_unused_reservation(reservation)
            return await self._fail(
                claimed,
                error_code=guard_decision.code or "distill_provider_cooldown",
                terminal=False,
                retry_after_seconds=max(
                    self._retry_delay(claimed.extraction_attempts),
                    guard_decision.remaining_seconds(time.time()),
                ),
            )
        try:
            started_at = self._clock()
            output = await self._backend.extract(extraction_input)
        except asyncio.CancelledError:
            await self._fail(
                claimed,
                error_code="distill_cancelled",
                terminal=False,
                retry_after_seconds=self._retry_delay(
                    claimed.extraction_attempts
                ),
                accounting=self._accounting(
                    snapshot_bytes=snapshot.byte_count,
                    started_at=started_at,
                    estimated_max_tokens=estimated_max_tokens,
                ),
            )
            raise
        except Exception as error:
            error_code, terminal = _body_free_error_code(error)
            retry_after_seconds = self._retry_delay_after_error(
                error_code=error_code,
                terminal=terminal,
                extraction_attempt=claimed.extraction_attempts,
            )
            # Status only — never provider output. A nonzero exit otherwise
            # reaches the journal as one opaque code whatever the cause was
            # (#760 was a schema rejection and had to be reproduced by hand).
            # Recorded codes are unchanged; this is a log line beside them.
            exit_status = getattr(error, "exit_status", None)
            if isinstance(exit_status, int):
                logger.warning(
                    "Distill extraction failed: code=%s terminal=%s provider_exit_status=%d",
                    error_code,
                    terminal,
                    exit_status,
                )
            return await self._fail(
                claimed,
                error_code=error_code,
                terminal=terminal,
                retry_after_seconds=retry_after_seconds,
                accounting=self._accounting(
                    snapshot_bytes=snapshot.byte_count,
                    started_at=started_at,
                    estimated_max_tokens=estimated_max_tokens,
                ),
            )
        accounting = self._accounting(
            snapshot_bytes=snapshot.byte_count,
            started_at=started_at,
            estimated_max_tokens=estimated_max_tokens,
        )
        if not isinstance(output, DistillExtractionOutput):
            return await self._fail(
                claimed,
                error_code="distill_output_invalid",
                terminal=True,
                accounting=accounting,
            )
        # Provider-neutral defense in depth: concrete Claude/Codex/Piri
        # adapters enforce this contract, but the worker is the final common
        # boundary before every sink. A future DistillBackend must not be able
        # to reintroduce reasonless live decisions by omitting adapter-local
        # validation.
        try:
            output = validate_live_decision_reasons(output)
        except DecisionReasonMissingError:
            return await self._fail(
                claimed,
                error_code="distill_decision_reason_missing",
                terminal=False,
                retry_after_seconds=self._retry_delay(
                    claimed.extraction_attempts
                ),
                accounting=accounting,
            )
        provenance = output.provenance
        if (
            provenance.provider != extraction_input.provider
            or provenance.source_thread_hash != extraction_input.source_thread_hash
            or provenance.trigger != extraction_input.trigger
        ):
            return await self._fail(
                claimed,
                error_code="distill_output_provenance_invalid",
                terminal=True,
                accounting=accounting,
            )
        if not self._wiki_enabled and output.wiki_candidates:
            return await self._fail(
                claimed,
                error_code="distill_output_wiki_disabled",
                terminal=True,
                accounting=accounting,
            )
        return await asyncio.to_thread(
            self._journal.mark_extraction_done,
            claimed.job_id,
            owner_token=self._owner_token,
            lease_epoch=claimed.extraction_lease_epoch,
            extraction_output=output,
            accounting=accounting,
            wiki_enabled=self._wiki_enabled,
            honcho_enabled=self._honcho_enabled,
        )


__all__ = ["CodexDistillExtractionWorker"]
