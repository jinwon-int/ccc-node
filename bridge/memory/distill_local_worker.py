"""Leased runtime worker for replay-safe Codex local memory write-back."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import secrets

from .distill_extraction import parse_extraction_output
from .distill_journal import DistillJournal
from .distill_local_sink import CodexLocalMemorySink
from .distill_types import DistillJob


class CodexDistillLocalSinkWorker:
    """Apply one retained extraction to its journal-bound audience scope."""

    def __init__(
        self,
        journal: DistillJournal,
        *,
        audience_root: Path,
        owner_token: str | None = None,
        lease_seconds: int = 300,
        max_attempts: int = 5,
        max_facts: int = 1000,
        max_resume_bytes: int = 4000,
    ) -> None:
        if lease_seconds <= 0 or max_attempts <= 0:
            raise ValueError("invalid local sink worker lease configuration")
        if max_facts <= 0 or max_resume_bytes < 256:
            raise ValueError("invalid local sink worker bound configuration")
        self._journal = journal
        self._audience_root = Path(os.path.abspath(os.fspath(audience_root)))
        self._owner_token = owner_token or secrets.token_hex(16)
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts
        self._max_facts = max_facts
        self._max_resume_bytes = max_resume_bytes

    async def _fail(
        self,
        claimed: DistillJob,
        *,
        error_code: str,
        terminal: bool,
    ) -> DistillJob:
        method = (
            self._journal.mark_local_sink_terminal_failed
            if terminal
            else self._journal.mark_local_sink_retryable_failed
        )
        return await asyncio.to_thread(
            method,
            claimed.job_id,
            owner_token=self._owner_token,
            lease_epoch=claimed.local_sink_lease_epoch,
            error_code=error_code,
        )

    def _sink_for(self, claimed: DistillJob) -> CodexLocalMemorySink:
        audience = claimed.memory_audience
        scope = claimed.memory_scope
        if audience not in {"private", "shared"} or scope is None:
            raise ValueError("local sink job has no safe audience route")
        state_dir = self._audience_root / scope / "state"
        if state_dir.parent.parent != self._audience_root:
            raise PermissionError("local sink scope escaped its audience root")
        return CodexLocalMemorySink(
            state_dir,
            audience=audience,
            max_facts=self._max_facts,
            max_resume_bytes=self._max_resume_bytes,
        )

    async def write_once(self, *, job_id: str) -> DistillJob:
        claimed = await asyncio.to_thread(
            self._journal.claim_local_sink,
            job_id,
            owner_token=self._owner_token,
            lease_seconds=self._lease_seconds,
            max_attempts=self._max_attempts,
        )
        if claimed is None:
            return await asyncio.to_thread(self._journal.get, job_id)
        try:
            if claimed.extraction_output is None:
                return await self._fail(
                    claimed,
                    error_code="local_sink_output_missing",
                    terminal=True,
                )
            output = parse_extraction_output(
                claimed.extraction_output,
                wiki_enabled=True,
            )
            sink = self._sink_for(claimed)
            await asyncio.to_thread(sink.write, output, job_id=claimed.job_id)
        except asyncio.CancelledError:
            await self._fail(
                claimed,
                error_code="local_sink_cancelled",
                terminal=False,
            )
            raise
        except (PermissionError, NotADirectoryError):
            return await self._fail(
                claimed,
                error_code="local_sink_path_unsafe",
                terminal=True,
            )
        except ValueError:
            return await self._fail(
                claimed,
                error_code="local_sink_output_invalid",
                terminal=True,
            )
        except OSError:
            return await self._fail(
                claimed,
                error_code="local_sink_io_failed",
                terminal=False,
            )
        except Exception:
            return await self._fail(
                claimed,
                error_code="local_sink_failed",
                terminal=False,
            )
        return await asyncio.to_thread(
            self._journal.mark_local_sink_done,
            claimed.job_id,
            owner_token=self._owner_token,
            lease_epoch=claimed.local_sink_lease_epoch,
        )


__all__ = ["CodexDistillLocalSinkWorker"]
