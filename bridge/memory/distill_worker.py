"""Replay-safe worker for the isolated Codex distill extraction stage."""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
from typing import Protocol

from .distill_extraction import (
    DistillBackend,
    DistillExtractionOutput,
    build_extraction_input,
)
from .distill_journal import DistillJournal
from .distill_types import DistillJob

logger = logging.getLogger(__name__)


class _BudgetDecisionLike(Protocol):
    @property
    def allowed(self) -> bool: ...

    def reason(self) -> str: ...


class AutonomousSpendGate(Protocol):
    """Structural view of core.usage_meter.UsageMeter used by this worker.

    Declared here as a Protocol so the memory package does not import the
    core package (keeping the bridge internal import graph acyclic).
    """

    def check_autonomous_spend(self, provider: str) -> _BudgetDecisionLike: ...

    def record(
        self,
        provider: str,
        mode: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        requests: int = 0,
    ) -> object: ...

_SAFE_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_RETRYABLE_BACKEND_CODES = frozenset(
    {
        "codex_distill_spawn_failed",
        "codex_distill_timeout",
        "codex_distill_io_failed",
        "codex_distill_nonzero_exit",
    }
)
_TERMINAL_BACKEND_CODES = frozenset(
    {
        "codex_distill_config_invalid",
        "codex_distill_input_invalid",
        "codex_distill_schema_unsafe",
        "codex_distill_executable_unsafe",
        "codex_distill_output_missing",
        "codex_distill_output_unsafe",
        "codex_distill_output_too_large",
        "codex_distill_output_invalid",
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
        owner_token: str | None = None,
        lease_seconds: int = 300,
        max_attempts: int = 5,
        wiki_enabled: bool = True,
        usage_meter: AutonomousSpendGate | None = None,
    ) -> None:
        if lease_seconds <= 0 or max_attempts <= 0 or type(wiki_enabled) is not bool:
            raise ValueError("invalid distill extraction worker configuration")
        self._journal = journal
        self._backend = backend
        self._owner_token = owner_token or secrets.token_hex(16)
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts
        self._wiki_enabled = wiki_enabled
        self._usage_meter = usage_meter

    async def _fail(
        self,
        claimed: DistillJob,
        *,
        error_code: str,
        terminal: bool,
    ) -> DistillJob:
        method = (
            self._journal.mark_extraction_terminal_failed
            if terminal
            else self._journal.mark_extraction_retryable_failed
        )
        return await asyncio.to_thread(
            method,
            claimed.job_id,
            owner_token=self._owner_token,
            lease_epoch=claimed.extraction_lease_epoch,
            error_code=error_code,
        )

    async def extract_once(self, *, job_id: str) -> DistillJob:
        if self._usage_meter is not None:
            decision = self._usage_meter.check_autonomous_spend("codex")
            if not decision.allowed:
                # Budget-blocked autonomous spend (#388): leave the job
                # unclaimed so no attempt is burned; it replays untouched
                # once the daily budget window resets or the cap is raised.
                logger.warning(
                    "Distill extraction deferred by usage budget: %s",
                    decision.reason(),
                )
                return await asyncio.to_thread(self._journal.get, job_id)
        claimed = await asyncio.to_thread(
            self._journal.claim_extraction,
            job_id,
            owner_token=self._owner_token,
            lease_seconds=self._lease_seconds,
            max_attempts=self._max_attempts,
        )
        if claimed is None:
            return await asyncio.to_thread(self._journal.get, job_id)
        snapshot = claimed.snapshot
        if snapshot is None or snapshot.thread_hash != claimed.thread_hash:
            return await self._fail(
                claimed,
                error_code="snapshot_thread_mismatch",
                terminal=True,
            )
        try:
            extraction_input = build_extraction_input(snapshot, trigger=claimed.trigger)
        except (TypeError, ValueError):
            return await self._fail(
                claimed,
                error_code="distill_input_invalid",
                terminal=True,
            )
        if self._usage_meter is not None:
            try:
                self._usage_meter.record("codex", "autonomous", requests=1)
            except Exception:
                logger.exception("Autonomous usage metering failed; extraction continues")
        try:
            output = await self._backend.extract(extraction_input)
        except asyncio.CancelledError:
            await self._fail(
                claimed,
                error_code="distill_cancelled",
                terminal=False,
            )
            raise
        except Exception as error:
            error_code, terminal = _body_free_error_code(error)
            return await self._fail(
                claimed,
                error_code=error_code,
                terminal=terminal,
            )
        if not isinstance(output, DistillExtractionOutput):
            return await self._fail(
                claimed,
                error_code="distill_output_invalid",
                terminal=True,
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
            )
        if not self._wiki_enabled and output.wiki_candidates:
            return await self._fail(
                claimed,
                error_code="distill_output_wiki_disabled",
                terminal=True,
            )
        return await asyncio.to_thread(
            self._journal.mark_extraction_done,
            claimed.job_id,
            owner_token=self._owner_token,
            lease_epoch=claimed.extraction_lease_epoch,
            extraction_output=output,
        )


__all__ = ["CodexDistillExtractionWorker"]
