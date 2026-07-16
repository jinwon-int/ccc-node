"""Owner-only atomic journal for Codex transcript snapshot triggers."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading
from typing import Any, Iterator

from telegram_bot.session.store import (
    _atomic_write_bytes,
    _fsync_directory,
    _validate_storage_directory,
    ensure_private_directory,
)

from .distill_types import (
    DISTILL_SCHEMA_VERSION,
    CodexTranscriptSnapshot,
    DistillJob,
    DistillJobStatus,
    DistillTrigger,
)
from .distill_extraction import (
    DistillExtractionOutput,
    parse_extraction_output,
)

_JOB_ID_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_MAX_JOB_BYTES = 1024 * 1024
_DEFAULT_DISCRIMINATOR = "session-close-v1"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_time(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return _normalize_time(value).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


class DistillJournal:
    """One process-safe and cross-process-locked directory of JSON job records."""

    def __init__(self, root: Path) -> None:
        self.root = Path(os.path.abspath(os.fspath(root)))
        self._thread_lock = threading.RLock()
        self._lock_path = self.root / ".journal.lock"
        self._initialized = False

    def validate_path(self) -> None:
        _validate_storage_directory(self.root)
        if self.root.exists():
            self._validate_root()
        if self._lock_path.exists() or self._lock_path.is_symlink():
            self._validate_regular_file(self._lock_path)
        for path in self.root.glob("*.json") if self.root.exists() else ():
            self._validate_job_name(path)
            self._validate_regular_file(path)

    def initialize(self) -> None:
        if self._initialized:
            return
        ensure_private_directory(self.root)
        self._validate_root()
        lock_existed = self._lock_path.exists() or self._lock_path.is_symlink()
        if lock_existed:
            self._validate_regular_file(self._lock_path)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self._lock_path, flags, 0o600)
        try:
            if not lock_existed:
                os.fchmod(fd, 0o600)
            self._validate_fd(fd, self._lock_path)
            os.fsync(fd)
        finally:
            os.close(fd)
        _fsync_directory(self.root)
        self._initialized = True

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise RuntimeError("DistillJournal is not initialized")

    def _validate_root(self) -> None:
        metadata = self.root.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise PermissionError(f"distill journal root must be a directory: {self.root}")
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise PermissionError("distill journal root is not owned by this process")
        mode = stat.S_IMODE(metadata.st_mode)
        if mode != 0o700:
            raise PermissionError(
                f"distill journal root must have mode 0700, got {mode:04o}"
            )

    @staticmethod
    def _validate_fd(fd: int, path: Path) -> None:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise PermissionError(f"distill journal state must be a regular file: {path}")
        if metadata.st_nlink != 1:
            raise PermissionError(f"distill journal state must not have hard links: {path}")
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise PermissionError("distill journal state is not owned by this process")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise PermissionError(f"distill journal state must have mode 0600: {path}")

    def _validate_regular_file(self, path: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
        except OSError as error:
            raise PermissionError(f"unsafe distill journal state: {path}") from error
        try:
            self._validate_fd(fd, path)
        finally:
            os.close(fd)

    @staticmethod
    def _validate_job_name(path: Path) -> None:
        if path.suffix != ".json" or not _JOB_ID_RE.fullmatch(path.stem):
            raise PermissionError(f"invalid distill journal job path: {path.name}")

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        self._require_initialized()
        with self._thread_lock:
            self._validate_root()
            flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(self._lock_path, flags)
            try:
                self._validate_fd(fd, self._lock_path)
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)

    @staticmethod
    def _job_id(
        provider: str,
        thread_id: str,
        discriminator: str,
        schema_version: int,
    ) -> str:
        material = json.dumps(
            [provider, thread_id, discriminator, schema_version],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(material).hexdigest()

    def job_path(self, job_id: str) -> Path:
        if not _JOB_ID_RE.fullmatch(job_id):
            raise ValueError("invalid distill job id")
        return self.root / f"{job_id}.json"

    def _read_unlocked(self, job_id: str) -> DistillJob:
        path = self.job_path(job_id)
        self._validate_regular_file(path)
        payload = path.read_bytes()
        if len(payload) > _MAX_JOB_BYTES:
            raise ValueError("distill job exceeds maximum journal record size")
        value = json.loads(payload.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("distill job record must be an object")
        job = DistillJob.from_dict(value)
        if job.job_id != job_id:
            raise ValueError("distill job id does not match its path")
        expected_job_id = self._job_id(
            job.provider,
            job.thread_id,
            job.discriminator,
            job.schema_version,
        )
        if expected_job_id != job.job_id:
            raise ValueError("distill job content does not match its id")
        extraction_statuses = {
            DistillJobStatus.RUNNING_EXTRACTION,
            DistillJobStatus.EXTRACTION_RETRYABLE_FAILED,
            DistillJobStatus.EXTRACTION_DONE,
            DistillJobStatus.EXTRACTION_TERMINAL_FAILED,
        }
        if job.status in extraction_statuses and job.snapshot is None:
            raise ValueError("distill extraction job is missing its snapshot")
        if (job.status is DistillJobStatus.EXTRACTION_DONE) != (
            job.extraction_output is not None
        ):
            raise ValueError("distill extraction output does not match job status")
        if job.extraction_output is not None:
            try:
                parse_extraction_output(job.extraction_output, wiki_enabled=True)
            except (TypeError, ValueError):
                raise ValueError("invalid distill job extraction output") from None
        return job

    def _write_unlocked(self, job: DistillJob) -> None:
        payload = json.dumps(
            job.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        if len(payload) > _MAX_JOB_BYTES:
            raise ValueError("distill job exceeds maximum journal record size")
        path = self.job_path(job.job_id)
        if path.exists() or path.is_symlink():
            self._validate_regular_file(path)
        _atomic_write_bytes(path, payload)
        self._validate_regular_file(path)

    def enqueue_once(
        self,
        *,
        provider: str,
        thread_id: str,
        trigger: DistillTrigger,
        discriminator: str = _DEFAULT_DISCRIMINATOR,
        schema_version: int = DISTILL_SCHEMA_VERSION,
        now: datetime | None = None,
    ) -> DistillJob:
        if provider != "codex":
            raise ValueError("distill journal accepts Codex jobs only")
        if not isinstance(thread_id, str) or not thread_id:
            raise ValueError("thread_id must not be empty")
        if not discriminator:
            raise ValueError("discriminator must not be empty")
        trigger = DistillTrigger(trigger)
        timestamp = _timestamp(now or _utc_now())
        thread_hash = hashlib.sha256(thread_id.encode("utf-8")).hexdigest()
        job_id = self._job_id(provider, thread_id, discriminator, schema_version)
        path = self.job_path(job_id)
        with self._exclusive():
            if path.exists() or path.is_symlink():
                existing = self._read_unlocked(job_id)
                if (
                    existing.provider != provider
                    or existing.thread_id != thread_id
                    or existing.discriminator != discriminator
                    or existing.schema_version != schema_version
                ):
                    raise RuntimeError("distill job hash collision")
                return existing
            job = DistillJob(
                job_id=job_id,
                provider=provider,
                thread_id=thread_id,
                thread_hash=thread_hash,
                discriminator=discriminator,
                trigger=trigger,
                status=DistillJobStatus.QUEUED,
                schema_version=schema_version,
                created_at=timestamp,
                updated_at=timestamp,
            )
            self._write_unlocked(job)
            return job

    def get(self, job_id: str) -> DistillJob:
        with self._exclusive():
            return self._read_unlocked(job_id)

    def list_jobs(self) -> tuple[DistillJob, ...]:
        with self._exclusive():
            paths = sorted(self.root.glob("*.json"))
            for path in paths:
                self._validate_job_name(path)
            return tuple(self._read_unlocked(path.stem) for path in paths)

    def claim(
        self,
        job_id: str,
        *,
        owner_token: str,
        now: datetime | None = None,
        lease_seconds: int = 300,
        max_attempts: int = 5,
    ) -> DistillJob | None:
        if not owner_token:
            raise ValueError("owner_token must not be empty")
        if lease_seconds <= 0 or max_attempts <= 0:
            raise ValueError("lease_seconds and max_attempts must be positive")
        current_time = _normalize_time(now or _utc_now())
        with self._exclusive():
            job = self._read_unlocked(job_id)
            if job.status not in {
                DistillJobStatus.QUEUED,
                DistillJobStatus.RETRYABLE_FAILED,
            }:
                return None
            if job.attempts >= max_attempts:
                terminal = replace(
                    job,
                    status=DistillJobStatus.TERMINAL_FAILED,
                    updated_at=_timestamp(current_time),
                    owner_token=None,
                    lease_expires_at=None,
                    error_code="max_attempts_exceeded",
                )
                self._write_unlocked(terminal)
                return None
            running = replace(
                job,
                status=DistillJobStatus.RUNNING_SNAPSHOT,
                updated_at=_timestamp(current_time),
                attempts=job.attempts + 1,
                lease_epoch=job.lease_epoch + 1,
                owner_token=owner_token,
                lease_expires_at=_timestamp(
                    current_time + timedelta(seconds=lease_seconds)
                ),
                error_code=None,
            )
            self._write_unlocked(running)
            return running

    @staticmethod
    def _validate_error_code(error_code: str) -> None:
        if not _SAFE_ERROR_CODE_RE.fullmatch(error_code):
            raise ValueError("error_code must be a safe diagnostic token")

    @staticmethod
    def _require_running(
        job: DistillJob, owner_token: str, lease_epoch: int
    ) -> None:
        if job.status is not DistillJobStatus.RUNNING_SNAPSHOT:
            raise RuntimeError(
                f"invalid distill job transition from {job.status.value}"
            )
        if job.owner_token != owner_token or job.lease_epoch != lease_epoch:
            raise RuntimeError("distill job owner or lease epoch does not match")

    def mark_snapshot_done(
        self,
        job_id: str,
        *,
        owner_token: str,
        lease_epoch: int,
        snapshot: CodexTranscriptSnapshot,
        now: datetime | None = None,
    ) -> DistillJob:
        with self._exclusive():
            job = self._read_unlocked(job_id)
            self._require_running(job, owner_token, lease_epoch)
            done = replace(
                job,
                status=DistillJobStatus.SNAPSHOT_DONE,
                updated_at=_timestamp(now or _utc_now()),
                owner_token=None,
                lease_expires_at=None,
                snapshot=snapshot,
                error_code=None,
            )
            self._write_unlocked(done)
            return done

    def claim_extraction(
        self,
        job_id: str,
        *,
        owner_token: str,
        now: datetime | None = None,
        lease_seconds: int = 300,
        max_attempts: int = 5,
    ) -> DistillJob | None:
        if not owner_token:
            raise ValueError("owner_token must not be empty")
        if lease_seconds <= 0 or max_attempts <= 0:
            raise ValueError("lease_seconds and max_attempts must be positive")
        current_time = _normalize_time(now or _utc_now())
        with self._exclusive():
            job = self._read_unlocked(job_id)
            if job.status not in {
                DistillJobStatus.SNAPSHOT_DONE,
                DistillJobStatus.EXTRACTION_RETRYABLE_FAILED,
            }:
                return None
            if job.snapshot is None:
                raise ValueError("distill extraction job is missing its snapshot")
            if job.extraction_attempts >= max_attempts:
                terminal = replace(
                    job,
                    status=DistillJobStatus.EXTRACTION_TERMINAL_FAILED,
                    updated_at=_timestamp(current_time),
                    owner_token=None,
                    lease_expires_at=None,
                    error_code="extraction_max_attempts_exceeded",
                )
                self._write_unlocked(terminal)
                return None
            running = replace(
                job,
                status=DistillJobStatus.RUNNING_EXTRACTION,
                updated_at=_timestamp(current_time),
                extraction_attempts=job.extraction_attempts + 1,
                extraction_lease_epoch=job.extraction_lease_epoch + 1,
                owner_token=owner_token,
                lease_expires_at=_timestamp(
                    current_time + timedelta(seconds=lease_seconds)
                ),
                error_code=None,
            )
            self._write_unlocked(running)
            return running

    @staticmethod
    def _require_running_extraction(
        job: DistillJob, owner_token: str, lease_epoch: int
    ) -> None:
        if job.status is not DistillJobStatus.RUNNING_EXTRACTION:
            raise RuntimeError(
                f"invalid distill job transition from {job.status.value}"
            )
        if (
            job.owner_token != owner_token
            or job.extraction_lease_epoch != lease_epoch
        ):
            raise RuntimeError("distill job owner or lease epoch does not match")

    @staticmethod
    def _canonical_extraction_output(output: DistillExtractionOutput) -> str:
        if not isinstance(output, DistillExtractionOutput):
            raise ValueError("extraction_output must be a DistillExtractionOutput")
        return json.dumps(
            output.model_dump(mode="json"),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def mark_extraction_done(
        self,
        job_id: str,
        *,
        owner_token: str,
        lease_epoch: int,
        extraction_output: DistillExtractionOutput,
        now: datetime | None = None,
    ) -> DistillJob:
        payload = self._canonical_extraction_output(extraction_output)
        payload_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        with self._exclusive():
            job = self._read_unlocked(job_id)
            self._require_running_extraction(job, owner_token, lease_epoch)
            if job.snapshot is None:
                raise ValueError("distill extraction job is missing its snapshot")
            provenance = extraction_output.provenance
            if (
                provenance.provider != job.provider
                or provenance.source_thread_hash != job.snapshot.thread_hash
                or provenance.trigger != job.trigger
            ):
                raise ValueError("distill extraction provenance does not match job")
            done = replace(
                job,
                status=DistillJobStatus.EXTRACTION_DONE,
                updated_at=_timestamp(now or _utc_now()),
                owner_token=None,
                lease_expires_at=None,
                extraction_output=payload,
                extraction_output_hash=payload_hash,
                error_code=None,
            )
            self._write_unlocked(done)
            return done

    def mark_extraction_retryable_failed(
        self,
        job_id: str,
        *,
        owner_token: str,
        lease_epoch: int,
        error_code: str,
        now: datetime | None = None,
    ) -> DistillJob:
        return self._mark_extraction_failed(
            job_id,
            owner_token=owner_token,
            lease_epoch=lease_epoch,
            error_code=error_code,
            status=DistillJobStatus.EXTRACTION_RETRYABLE_FAILED,
            now=now,
        )

    def mark_extraction_terminal_failed(
        self,
        job_id: str,
        *,
        owner_token: str,
        lease_epoch: int,
        error_code: str,
        now: datetime | None = None,
    ) -> DistillJob:
        return self._mark_extraction_failed(
            job_id,
            owner_token=owner_token,
            lease_epoch=lease_epoch,
            error_code=error_code,
            status=DistillJobStatus.EXTRACTION_TERMINAL_FAILED,
            now=now,
        )

    def _mark_extraction_failed(
        self,
        job_id: str,
        *,
        owner_token: str,
        lease_epoch: int,
        error_code: str,
        status: DistillJobStatus,
        now: datetime | None,
    ) -> DistillJob:
        self._validate_error_code(error_code)
        with self._exclusive():
            job = self._read_unlocked(job_id)
            self._require_running_extraction(job, owner_token, lease_epoch)
            failed = replace(
                job,
                status=status,
                updated_at=_timestamp(now or _utc_now()),
                owner_token=None,
                lease_expires_at=None,
                extraction_output=None,
                extraction_output_hash=None,
                error_code=error_code,
            )
            self._write_unlocked(failed)
            return failed

    def get_extraction_output(self, job_id: str) -> DistillExtractionOutput | None:
        job = self.get(job_id)
        if job.extraction_output is None:
            return None
        return parse_extraction_output(job.extraction_output, wiki_enabled=True)

    def mark_retryable_failed(
        self,
        job_id: str,
        *,
        owner_token: str,
        lease_epoch: int,
        error_code: str,
        now: datetime | None = None,
    ) -> DistillJob:
        self._validate_error_code(error_code)
        with self._exclusive():
            job = self._read_unlocked(job_id)
            self._require_running(job, owner_token, lease_epoch)
            failed = replace(
                job,
                status=DistillJobStatus.RETRYABLE_FAILED,
                updated_at=_timestamp(now or _utc_now()),
                owner_token=None,
                lease_expires_at=None,
                error_code=error_code,
            )
            self._write_unlocked(failed)
            return failed

    def mark_terminal_failed(
        self,
        job_id: str,
        *,
        owner_token: str,
        lease_epoch: int,
        error_code: str,
        now: datetime | None = None,
    ) -> DistillJob:
        self._validate_error_code(error_code)
        with self._exclusive():
            job = self._read_unlocked(job_id)
            self._require_running(job, owner_token, lease_epoch)
            failed = replace(
                job,
                status=DistillJobStatus.TERMINAL_FAILED,
                updated_at=_timestamp(now or _utc_now()),
                owner_token=None,
                lease_expires_at=None,
                error_code=error_code,
            )
            self._write_unlocked(failed)
            return failed

    def recover_stale_running(self, *, now: datetime | None = None) -> int:
        current_time = _normalize_time(now or _utc_now())
        recovered = 0
        with self._exclusive():
            for path in sorted(self.root.glob("*.json")):
                self._validate_job_name(path)
                job = self._read_unlocked(path.stem)
                if job.status not in {
                    DistillJobStatus.RUNNING_SNAPSHOT,
                    DistillJobStatus.RUNNING_EXTRACTION,
                } or job.lease_expires_at is None:
                    continue
                try:
                    lease_expires = _parse_timestamp(job.lease_expires_at)
                except ValueError as error:
                    raise ValueError("invalid distill job lease timestamp") from error
                if lease_expires > current_time:
                    continue
                recovery_status = (
                    DistillJobStatus.QUEUED
                    if job.status is DistillJobStatus.RUNNING_SNAPSHOT
                    else DistillJobStatus.EXTRACTION_RETRYABLE_FAILED
                )
                error_code = (
                    "lease_expired"
                    if job.status is DistillJobStatus.RUNNING_SNAPSHOT
                    else "extraction_lease_expired"
                )
                queued = replace(
                    job,
                    status=recovery_status,
                    updated_at=_timestamp(current_time),
                    owner_token=None,
                    lease_expires_at=None,
                    error_code=error_code,
                )
                self._write_unlocked(queued)
                recovered += 1
        return recovered

    def diagnostics(self, job_id: str) -> dict[str, Any]:
        job = self.get(job_id)
        snapshot = job.snapshot
        extraction = (
            parse_extraction_output(job.extraction_output, wiki_enabled=True)
            if job.extraction_output is not None
            else None
        )
        return {
            "job_id": job.job_id,
            "thread_hash": job.thread_hash,
            "trigger": job.trigger.value,
            "status": job.status.value,
            "attempts": job.attempts,
            "lease_epoch": job.lease_epoch,
            "extraction_attempts": job.extraction_attempts,
            "extraction_lease_epoch": job.extraction_lease_epoch,
            "message_count": len(snapshot.messages) if snapshot is not None else 0,
            "byte_count": snapshot.byte_count if snapshot is not None else 0,
            "truncated": snapshot.truncated if snapshot is not None else False,
            "extraction_output_bytes": (
                len(job.extraction_output.encode("utf-8"))
                if job.extraction_output is not None
                else 0
            ),
            "honcho_count": len(extraction.honcho) if extraction is not None else 0,
            "wiki_candidate_count": (
                len(extraction.wiki_candidates) if extraction is not None else 0
            ),
            "error_code": job.error_code,
        }
