"""Replay-safe owner-only queue for human-reviewed Codex Wiki candidates."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import threading
from typing import Iterator

from telegram_bot.utils.secure_fs import _atomic_write_bytes, ensure_private_directory

from .distill_extraction import DistillExtractionOutput


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_RECORD_BYTES = 16 * 1024


@dataclass(frozen=True, slots=True)
class WikiCandidateWriteResult:
    candidates_queued: int
    record_written: bool


class WikiCandidateCollisionError(ValueError):
    """An existing job record differs from its immutable candidate payload."""


class CodexWikiCandidateSink:
    """Persist validated candidates for a later, explicit human review.

    One immutable JSON record is keyed by distill job id. This sink never
    invokes ``wiki-agent``, creates a branch or PR, or writes to a Wiki tree.
    """

    def __init__(self, queue_dir: Path) -> None:
        self.queue_dir = Path(os.path.abspath(os.fspath(queue_dir)))
        self._lock_path = self.queue_dir / ".wiki-candidate-sink.lock"
        self._thread_lock = threading.RLock()

    @staticmethod
    def _validate_regular_file(path: Path) -> None:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise PermissionError(f"Wiki candidate state must be regular: {path}")
        if metadata.st_nlink != 1:
            raise PermissionError("Wiki candidate state must not have hard links")
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise PermissionError("Wiki candidate state is not owned by this process")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise PermissionError("Wiki candidate state must have mode 0600")
        if metadata.st_size > _MAX_RECORD_BYTES:
            raise PermissionError("Wiki candidate state exceeds its safe read bound")

    @classmethod
    def _validate_open_file(cls, descriptor: int, path: Path) -> None:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise PermissionError(f"Wiki candidate lock must be regular: {path}")
        if metadata.st_nlink != 1:
            raise PermissionError("Wiki candidate lock must not have hard links")
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise PermissionError("Wiki candidate lock is not owned by this process")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise PermissionError("Wiki candidate lock must have mode 0600")

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        ensure_private_directory(self.queue_dir)
        with self._thread_lock:
            self._validate_regular_file(self._lock_path)
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(self._lock_path, flags, 0o600)
            try:
                os.fchmod(descriptor, 0o600)
                self._validate_open_file(descriptor, self._lock_path)
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                yield
            finally:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)

    @staticmethod
    def _payload(output: DistillExtractionOutput, *, job_id: str) -> bytes:
        provenance = output.provenance
        record = {
            "schema_version": output.schema_version,
            "job_id": job_id,
            "review_status": "pending",
            "provenance": {
                "provider": provenance.provider,
                "source_thread_hash": provenance.source_thread_hash,
                "trigger": provenance.trigger.value,
                "distilled_at": provenance.distilled_at,
            },
            "candidates": [
                candidate.model_dump(mode="json")
                for candidate in output.wiki_candidates
            ],
        }
        return json.dumps(
            record,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def write(
        self,
        output: DistillExtractionOutput,
        *,
        job_id: str,
    ) -> WikiCandidateWriteResult:
        if not isinstance(output, DistillExtractionOutput):
            raise ValueError("output must be a validated DistillExtractionOutput")
        if not isinstance(job_id, str) or not _SHA256_RE.fullmatch(job_id):
            raise ValueError("job_id must be a SHA-256 hex digest")
        candidates_queued = len(output.wiki_candidates)
        with self._exclusive():
            if candidates_queued == 0:
                return WikiCandidateWriteResult(0, False)
            path = self.queue_dir / f"{job_id}.json"
            payload = self._payload(output, job_id=job_id)
            if len(payload) > _MAX_RECORD_BYTES:
                raise ValueError("Wiki candidate record exceeds its write bound")
            self._validate_regular_file(path)
            if path.exists():
                if path.read_bytes() == payload:
                    return WikiCandidateWriteResult(candidates_queued, False)
                raise WikiCandidateCollisionError("Wiki candidate job collision")
            _atomic_write_bytes(path, payload)
            self._validate_regular_file(path)
            return WikiCandidateWriteResult(candidates_queued, True)


__all__ = [
    "CodexWikiCandidateSink",
    "WikiCandidateCollisionError",
    "WikiCandidateWriteResult",
]
