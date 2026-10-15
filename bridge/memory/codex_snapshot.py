"""Read-only Codex thread snapshot worker for queued distill jobs."""

from __future__ import annotations

import asyncio
import logging
from typing import Mapping, Protocol
import secrets

from .distill_journal import DistillJournal
from .distill_types import CodexTranscriptSnapshot, DistillJob, TranscriptBounds


logger = logging.getLogger(__name__)


def _refusal_reason(exc: ValueError) -> str:
    if type(exc) is ValueError:
        return str(exc).splitlines()[0][:200] if str(exc) else "ValueError"
    return type(exc).__name__


def _normalize_provider(value: str) -> str:
    return str(value).strip().lower()


class SnapshotRuntime(Protocol):
    async def read_session_snapshot(
        self,
        session_id: str,
        *,
        bounds: TranscriptBounds,
        memory_audience: str | None = None,
        memory_scope: str | None = None,
    ) -> CodexTranscriptSnapshot: ...


class CodexThreadSnapshotter:
    """Claim one journal job and populate it using only the runtime read API.

    ``provider`` names the provider ``runtime`` serves. When it is set, a job
    is read only by a reader for the job's own provider: ``runtime`` for a
    matching job, otherwise the read-only reader registered for that provider
    in ``provider_readers``. A job with no such reader is left retryable
    (``snapshot_provider_mismatch``) and is never handed to another provider's
    reader (#2316): after a codex->claude switch the departing Codex job's UUID
    thread id also passes the Claude session-id check, and the Claude reader
    used to return an empty snapshot for it, silently completing the job with
    no memory. Retrying keeps it in the journal; if the bridge does not serve
    that provider again in time it terminalizes as ``max_attempts_exceeded``.
    ``provider=None`` keeps the unbound single-runtime behaviour.
    """

    def __init__(
        self,
        journal: DistillJournal,
        runtime: SnapshotRuntime,
        *,
        bounds: TranscriptBounds | None = None,
        owner_token: str | None = None,
        lease_seconds: int = 300,
        provider: str | None = None,
        provider_readers: Mapping[str, SnapshotRuntime] | None = None,
    ) -> None:
        self._journal = journal
        self._runtime = runtime
        self._bounds = bounds or TranscriptBounds()
        self._owner_token = owner_token or secrets.token_hex(16)
        self._lease_seconds = lease_seconds
        self._provider = _normalize_provider(provider) if provider is not None else None
        if provider_readers and self._provider is None:
            raise ValueError("provider_readers require the runtime provider")
        readers = {
            _normalize_provider(name): reader
            for name, reader in (provider_readers or {}).items()
        }
        if self._provider in readers:
            raise ValueError("the runtime provider must not have a second reader")
        self._provider_readers = readers

    def _reader_for(self, job_provider: str) -> SnapshotRuntime | None:
        if self._provider is None:
            return self._runtime
        provider = _normalize_provider(job_provider)
        if provider == self._provider:
            return self._runtime
        return self._provider_readers.get(provider)

    async def snapshot_once(self, *, job_id: str) -> DistillJob:
        claimed = await asyncio.to_thread(
            self._journal.claim,
            job_id,
            owner_token=self._owner_token,
            lease_seconds=self._lease_seconds,
        )
        if claimed is None:
            return await asyncio.to_thread(self._journal.get, job_id)
        reader = self._reader_for(claimed.provider)
        if reader is None:
            logger.warning(
                "distill snapshot job %s (provider %s) has no %s reader in this "
                "bridge (serving %s); left retryable as snapshot_provider_mismatch "
                "— it ends as max_attempts_exceeded unless the bridge serves %s "
                "again first (#2316)",
                job_id[:12],
                claimed.provider,
                claimed.provider,
                self._provider,
                claimed.provider,
            )
            return await asyncio.to_thread(
                self._journal.mark_retryable_failed,
                job_id,
                owner_token=self._owner_token,
                lease_epoch=claimed.lease_epoch,
                error_code="snapshot_provider_mismatch",
            )
        try:
            if claimed.memory_audience is None:
                snapshot = await reader.read_session_snapshot(
                    claimed.thread_id,
                    bounds=self._bounds,
                )
            else:
                snapshot = await reader.read_session_snapshot(
                    claimed.thread_id,
                    bounds=self._bounds,
                    memory_audience=claimed.memory_audience,
                    memory_scope=claimed.memory_scope,
                )
        except ValueError as exc:
            # Terminal and never retried: say why, or a node silently loses
            # every session's memory (#2306 — an 0755 transcript directory
            # failed every job for days with no log line). Plain ValueError
            # reasons are reader/route messages (some name a local config path,
            # never transcript text); subclasses (pydantic ValidationError,
            # JSONDecodeError) can echo input, so only their class name is
            # logged.
            logger.warning(
                "distill snapshot refused job %s (provider %s): %s — job "
                "terminalized as invalid_snapshot_request",
                job_id[:12],
                claimed.provider,
                _refusal_reason(exc),
            )
            return await asyncio.to_thread(
                self._journal.mark_terminal_failed,
                job_id,
                owner_token=self._owner_token,
                lease_epoch=claimed.lease_epoch,
                error_code="invalid_snapshot_request",
            )
        except Exception:
            return await asyncio.to_thread(
                self._journal.mark_retryable_failed,
                job_id,
                owner_token=self._owner_token,
                lease_epoch=claimed.lease_epoch,
                error_code="snapshot_read_failed",
            )
        return await asyncio.to_thread(
            self._journal.mark_snapshot_done,
            job_id,
            owner_token=self._owner_token,
            lease_epoch=claimed.lease_epoch,
            snapshot=snapshot,
        )
