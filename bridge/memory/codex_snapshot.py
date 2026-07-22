"""Read-only Codex thread snapshot worker for queued distill jobs."""

from __future__ import annotations

import asyncio
from typing import Protocol
import secrets

from .distill_journal import DistillJournal
from .distill_types import CodexTranscriptSnapshot, DistillJob, TranscriptBounds


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
    """Claim one journal job and populate it using only the runtime read API."""

    def __init__(
        self,
        journal: DistillJournal,
        runtime: SnapshotRuntime,
        *,
        bounds: TranscriptBounds | None = None,
        owner_token: str | None = None,
        lease_seconds: int = 300,
    ) -> None:
        self._journal = journal
        self._runtime = runtime
        self._bounds = bounds or TranscriptBounds()
        self._owner_token = owner_token or secrets.token_hex(16)
        self._lease_seconds = lease_seconds

    async def snapshot_once(self, *, job_id: str) -> DistillJob:
        claimed = await asyncio.to_thread(
            self._journal.claim,
            job_id,
            owner_token=self._owner_token,
            lease_seconds=self._lease_seconds,
        )
        if claimed is None:
            return await asyncio.to_thread(self._journal.get, job_id)
        try:
            if claimed.memory_audience is None:
                snapshot = await self._runtime.read_session_snapshot(
                    claimed.thread_id,
                    bounds=self._bounds,
                )
            else:
                snapshot = await self._runtime.read_session_snapshot(
                    claimed.thread_id,
                    bounds=self._bounds,
                    memory_audience=claimed.memory_audience,
                    memory_scope=claimed.memory_scope,
                )
        except ValueError:
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
