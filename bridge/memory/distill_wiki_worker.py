"""Leased runtime worker for the human-gated Codex Wiki candidate queue."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import secrets

from .distill_extraction import parse_extraction_output
from .distill_journal import DistillJournal
from .distill_types import DistillJob
from .distill_wiki_sink import (
    CodexWikiCandidateSink,
    WikiCandidateCollisionError,
)


class CodexDistillWikiSinkWorker:
    """Apply one retained extraction to a local, human-reviewed queue."""

    def __init__(
        self,
        journal: DistillJournal,
        *,
        queue_dir: Path,
        owner_token: str | None = None,
        lease_seconds: int = 300,
        max_attempts: int = 5,
    ) -> None:
        if lease_seconds <= 0 or max_attempts <= 0:
            raise ValueError("invalid Wiki sink worker lease configuration")
        self._journal = journal
        self._sink = CodexWikiCandidateSink(
            Path(os.path.abspath(os.fspath(queue_dir)))
        )
        self._owner_token = owner_token or secrets.token_hex(16)
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts

    async def _fail(
        self,
        claimed: DistillJob,
        *,
        error_code: str,
        terminal: bool,
    ) -> DistillJob:
        method = (
            self._journal.mark_wiki_sink_terminal_failed
            if terminal
            else self._journal.mark_wiki_sink_retryable_failed
        )
        return await asyncio.to_thread(
            method,
            claimed.job_id,
            owner_token=self._owner_token,
            lease_epoch=claimed.wiki_sink_lease_epoch,
            error_code=error_code,
        )

    async def write_once(self, *, job_id: str) -> DistillJob:
        claimed = await asyncio.to_thread(
            self._journal.claim_wiki_sink,
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
                    error_code="wiki_sink_output_missing",
                    terminal=True,
                )
            output = parse_extraction_output(
                claimed.extraction_output,
                wiki_enabled=True,
            )
            await asyncio.to_thread(
                self._sink.write,
                output,
                job_id=claimed.job_id,
            )
        except asyncio.CancelledError:
            await self._fail(
                claimed,
                error_code="wiki_sink_cancelled",
                terminal=False,
            )
            raise
        except WikiCandidateCollisionError:
            return await self._fail(
                claimed,
                error_code="wiki_sink_record_collision",
                terminal=True,
            )
        except (PermissionError, NotADirectoryError):
            return await self._fail(
                claimed,
                error_code="wiki_sink_path_unsafe",
                terminal=True,
            )
        except ValueError:
            return await self._fail(
                claimed,
                error_code="wiki_sink_output_invalid",
                terminal=True,
            )
        except OSError:
            return await self._fail(
                claimed,
                error_code="wiki_sink_io_failed",
                terminal=False,
            )
        except Exception:
            return await self._fail(
                claimed,
                error_code="wiki_sink_failed",
                terminal=False,
            )
        return await asyncio.to_thread(
            self._journal.mark_wiki_sink_done,
            claimed.job_id,
            owner_token=self._owner_token,
            lease_epoch=claimed.wiki_sink_lease_epoch,
        )


__all__ = ["CodexDistillWikiSinkWorker"]
