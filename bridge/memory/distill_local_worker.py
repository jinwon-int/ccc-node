"""Leased runtime worker for replay-safe Codex local memory write-back."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import math
import os
from pathlib import Path
import secrets
import signal
import stat

from .distill_extraction import parse_extraction_output
from .distill_journal import DistillJournal
from .distill_local_sink import CodexLocalMemorySink
from .distill_types import DistillJob


_INDEX_ENV_ALLOWLIST = (
    "HOME",
    "PATH",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
)
_MAX_INDEXER_BYTES = 1024 * 1024


class _LocalIndexError(RuntimeError):
    def __init__(self, *, terminal: bool) -> None:
        self.terminal = terminal
        super().__init__(
            "local_sink_index_unsafe" if terminal else "local_sink_index_failed"
        )


async def _stop_indexer(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except OSError:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(process.wait(), timeout=0.25)
        return
    except TimeoutError:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(process.wait(), timeout=1.0)
    except TimeoutError:
        pass


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
        indexer_path: str | Path | None = None,
        index_timeout_seconds: float = 30.0,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        if lease_seconds <= 0 or max_attempts <= 0:
            raise ValueError("invalid local sink worker lease configuration")
        if max_facts <= 0 or max_resume_bytes < 256:
            raise ValueError("invalid local sink worker bound configuration")
        if (
            not isinstance(index_timeout_seconds, (int, float))
            or isinstance(index_timeout_seconds, bool)
            or not math.isfinite(index_timeout_seconds)
            or index_timeout_seconds <= 0
            or index_timeout_seconds > 60
        ):
            raise ValueError("invalid local sink index timeout")
        self._journal = journal
        self._audience_root = Path(os.path.abspath(os.fspath(audience_root)))
        self._owner_token = owner_token or secrets.token_hex(16)
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts
        self._max_facts = max_facts
        self._max_resume_bytes = max_resume_bytes
        self._indexer_path = Path(indexer_path) if indexer_path is not None else None
        self._index_timeout_seconds = float(index_timeout_seconds)
        self._environment = dict(os.environ if environment is None else environment)

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

    def _validated_indexer(self) -> Path | None:
        if self._indexer_path is None:
            return None
        candidate = Path(os.path.abspath(os.fspath(self._indexer_path)))
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            # Setup drift is repairable; keep the already-durable fact pending
            # so installing the expected sibling helper can recover the job.
            raise _LocalIndexError(terminal=False) from None
        except OSError:
            raise _LocalIndexError(terminal=True) from None
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid not in {0, os.geteuid()}
            or stat.S_IMODE(metadata.st_mode) & 0o022
            or metadata.st_size <= 0
            or metadata.st_size > _MAX_INDEXER_BYTES
            or not os.access(candidate, os.X_OK)
        ):
            raise _LocalIndexError(terminal=True)
        return candidate

    def _index_environment(self, claimed: DistillJob) -> dict[str, str]:
        scope = claimed.memory_scope
        audience = claimed.memory_audience
        if audience not in {"private", "shared"} or scope is None:
            raise _LocalIndexError(terminal=True)
        scope_root = self._audience_root / scope
        state_dir = scope_root / "state"
        environment = {
            name: value
            for name in _INDEX_ENV_ALLOWLIST
            if isinstance((value := self._environment.get(name)), str)
            and "\x00" not in value
        }
        environment.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
        environment.update(
            {
                "CCC_MEMORY_AUDIENCE_SCOPED": "1",
                "CCC_MEMORY_AUDIENCE": audience,
                "CCC_MEMORY_SCOPE": scope,
                "CCC_MEMORY_AUDIENCE_ROOT": str(self._audience_root),
                "CCC_STATE_DIR": str(state_dir),
                "CCC_MEMORY_INDEX_DB": str(state_dir / "memory-index.sqlite"),
                "CCC_MEMORY_FACTS_FILE": str(state_dir / "memory-facts.jsonl"),
                "CCC_MEMORY_CACHE_DIR": str(scope_root / "cache"),
                "CCC_MEMORY_DIR": str(scope_root / "memories"),
                "CCC_MEMORY_INDEX_DISTILL": "0",
                "CCC_WIKI_MEMORY_ENABLED": "0",
                "CCC_HONCHO_MEMORY_ENABLED": "0",
                "CCC_MEMORY_NO_REFRESH": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        return environment

    async def _refresh_index(self, claimed: DistillJob) -> None:
        indexer = self._validated_indexer()
        if indexer is None:
            return
        try:
            process = await asyncio.create_subprocess_exec(
                str(indexer),
                "update",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env=self._index_environment(claimed),
                start_new_session=True,
            )
        except _LocalIndexError:
            raise
        except OSError:
            raise _LocalIndexError(terminal=False) from None
        try:
            await asyncio.wait_for(
                process.wait(),
                timeout=self._index_timeout_seconds,
            )
        except asyncio.CancelledError:
            await _stop_indexer(process)
            raise
        except TimeoutError:
            await _stop_indexer(process)
            raise _LocalIndexError(terminal=False) from None
        if process.returncode != 0:
            raise _LocalIndexError(terminal=False)

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
            await self._refresh_index(claimed)
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
        except _LocalIndexError as error:
            return await self._fail(
                claimed,
                error_code=str(error),
                terminal=error.terminal,
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
