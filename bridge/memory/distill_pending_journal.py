"""Typed durable journal for provider hook distill work.

The Claude hook predates the bridge journal and persists
``ccc.distill.pending.v1`` records.  This module keeps that on-disk contract and
its content-addressed identifiers stable while moving queue ownership out of
shell.  It is deliberately independent of provider execution: the journal
holds a claim while an arbitrary child command runs and removes a record only
after that command succeeds.

The module is also installed beside the Claude hooks.  Import fallbacks mirror
the other standalone memory helpers so source and installed copies share the
canonical secure-filesystem implementation.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from typing import Iterator, Mapping, Sequence

try:
    from telegram_bot.utils.secure_fs import (
        _atomic_write_bytes,
        _fsync_directory,
        _validate_storage_directory,
        ensure_private_directory,
    )
except ModuleNotFoundError:  # Standalone hook-tree copy installed by setup.sh.
    try:
        from ccc_secure_fs import (
            _atomic_write_bytes,
            _fsync_directory,
            _validate_storage_directory,
            ensure_private_directory,
        )
    except ModuleNotFoundError:  # Direct execution from a source checkout.
        secure_fs_source = Path(__file__).resolve().parents[1] / "utils/secure_fs.py"
        secure_fs_spec = importlib.util.spec_from_file_location(
            "ccc_secure_fs", secure_fs_source
        )
        if secure_fs_spec is None or secure_fs_spec.loader is None:
            raise
        secure_fs_module = importlib.util.module_from_spec(secure_fs_spec)
        sys.modules[secure_fs_spec.name] = secure_fs_module
        secure_fs_spec.loader.exec_module(secure_fs_module)
        _atomic_write_bytes = secure_fs_module._atomic_write_bytes
        _fsync_directory = secure_fs_module._fsync_directory
        _validate_storage_directory = secure_fs_module._validate_storage_directory
        ensure_private_directory = secure_fs_module.ensure_private_directory


PENDING_DISTILL_SCHEMA = "ccc.distill.pending.v1"
PENDING_DISTILL_ID_DISCRIMINATOR = "v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_TRIGGER_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_MAX_JOB_BYTES = 64 * 1024
_DISABLED_VALUES = frozenset({"0", "false", "FALSE", "off", "OFF", "no", "NO"})


class PendingDistillError(RuntimeError):
    """A body-free queue error safe to report from a fail-open hook."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class PendingDistillJob:
    """Validated legacy-v1 recovery metadata (never transcript content)."""

    job_id: str
    transcript_sha256: str
    session_id: str
    transcript_path: str
    source_cwd: str
    source_project: str
    trigger: str
    dryrun: int
    created_at: str
    isolation_profile: str
    wiki_memory_enabled: str
    memory_audience_scoped: str
    memory_audience: str
    memory_scope: str
    honcho_memory_enabled: str
    memory_user_label: str
    memory_assistant_label: str
    schema: str = PENDING_DISTILL_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != PENDING_DISTILL_SCHEMA:
            raise PendingDistillError("schema-unsupported")
        if not _SHA256_RE.fullmatch(self.job_id):
            raise PendingDistillError("job-id-invalid")
        if not _SHA256_RE.fullmatch(self.transcript_sha256):
            raise PendingDistillError("transcript-hash-invalid")
        for value in (self.session_id, self.transcript_path, self.created_at):
            if not value or "\x00" in value:
                raise PendingDistillError("required-field-invalid")
        for value in (
            self.source_cwd,
            self.source_project,
            self.isolation_profile,
            self.wiki_memory_enabled,
            self.memory_audience_scoped,
            self.memory_audience,
            self.memory_scope,
            self.honcho_memory_enabled,
            self.memory_user_label,
            self.memory_assistant_label,
        ):
            if "\x00" in value:
                raise PendingDistillError("environment-field-invalid")
        if not _SAFE_TRIGGER_RE.fullmatch(self.trigger):
            raise PendingDistillError("trigger-invalid")
        if self.dryrun not in {0, 1}:
            raise PendingDistillError("dryrun-invalid")
        expected = pending_distill_job_id(self.session_id, self.transcript_sha256)
        if expected != self.job_id:
            raise PendingDistillError("content-id-mismatch")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "job_id": self.job_id,
            "transcript_sha256": self.transcript_sha256,
            "session_id": self.session_id,
            "transcript_path": self.transcript_path,
            "source_cwd": self.source_cwd,
            "source_project": self.source_project,
            "trigger": self.trigger,
            "dryrun": self.dryrun,
            "created_at": self.created_at,
            "isolation_profile": self.isolation_profile,
            "wiki_memory_enabled": self.wiki_memory_enabled,
            "memory_audience_scoped": self.memory_audience_scoped,
            "memory_audience": self.memory_audience,
            "memory_scope": self.memory_scope,
            "honcho_memory_enabled": self.honcho_memory_enabled,
            "memory_user_label": self.memory_user_label,
            "memory_assistant_label": self.memory_assistant_label,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> PendingDistillJob:
        def string(name: str, default: str | None = None) -> str:
            raw = value.get(name, default)
            if not isinstance(raw, str):
                raise PendingDistillError(f"field-{name}-invalid")
            return raw

        raw_dryrun = value.get("dryrun", 0)
        if type(raw_dryrun) is not int:
            raise PendingDistillError("field-dryrun-invalid")
        return cls(
            schema=string("schema"),
            job_id=string("job_id"),
            transcript_sha256=string("transcript_sha256"),
            session_id=string("session_id"),
            transcript_path=string("transcript_path"),
            source_cwd=string("source_cwd", ""),
            source_project=string("source_project", ""),
            trigger=string("trigger", "manual"),
            dryrun=raw_dryrun,
            created_at=string("created_at"),
            isolation_profile=string("isolation_profile", "fleet"),
            wiki_memory_enabled=string("wiki_memory_enabled", "1"),
            memory_audience_scoped=string("memory_audience_scoped", "0"),
            memory_audience=string("memory_audience", "legacy"),
            memory_scope=string("memory_scope", ""),
            honcho_memory_enabled=string("honcho_memory_enabled", "1"),
            memory_user_label=string("memory_user_label", "Seo Jin On / 서진원"),
            memory_assistant_label=string(
                "memory_assistant_label", "dungae, a Hermes Team2 worker"
            ),
        )


@dataclass(frozen=True, slots=True)
class PendingDistillClaimResult:
    status: str
    job_id: str
    child_returncode: int | None = None


def pending_distill_job_id(session_id: str, transcript_sha256: str) -> str:
    """Return the historical v1 content ID used by ``distill.sh``."""
    material = (
        session_id.encode("utf-8")
        + b"\0"
        + transcript_sha256.encode("ascii")
        + b"\0"
        + PENDING_DISTILL_ID_DISCRIMINATOR.encode("ascii")
    )
    return hashlib.sha256(material).hexdigest()


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hash_transcript(path: Path) -> str:
    try:
        path_metadata = path.lstat()
    except OSError as error:
        raise PendingDistillError("transcript-open-failed") from error
    if stat.S_ISLNK(path_metadata.st_mode) or not stat.S_ISREG(path_metadata.st_mode):
        raise PendingDistillError("transcript-not-regular")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise PendingDistillError("transcript-open-failed") from error
    digest = hashlib.sha256()
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise PendingDistillError("transcript-not-regular")
        if (metadata.st_dev, metadata.st_ino) != (
            path_metadata.st_dev,
            path_metadata.st_ino,
        ):
            raise PendingDistillError("transcript-changed")
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _validate_transcript_reference(path: Path) -> None:
    """Reject a vanished/non-regular recovery target without reading its body."""
    try:
        metadata = path.lstat()
    except FileNotFoundError as error:
        raise PendingDistillError("transcript-missing") from error
    except OSError as error:
        raise PendingDistillError("transcript-stat-failed") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise PendingDistillError("transcript-not-regular")


class PendingDistillJournal:
    """Owner-only, cross-process pending distill queue."""

    def __init__(self, root: Path) -> None:
        self.root = Path(os.path.abspath(os.fspath(root)))
        self._lock_path = self.root / ".pending-journal.lock"

    def initialize(self) -> None:
        ensure_private_directory(self.root)
        self._validate_root()
        with self._open_lock(self._lock_path, nonblocking=False, lock=False):
            pass
        _fsync_directory(self.root)

    def validate_existing(self) -> None:
        self._validate_root()
        if self._lock_path.exists() or self._lock_path.is_symlink():
            with self._open_lock(self._lock_path, nonblocking=False, lock=False):
                pass

    def _validate_root(self) -> None:
        try:
            metadata = self.root.lstat()
        except OSError as error:
            raise PendingDistillError("pending-dir-missing") from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise PendingDistillError("pending-dir-unsafe")
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise PendingDistillError("pending-dir-owner")
        if stat.S_IMODE(metadata.st_mode) != 0o700:
            raise PendingDistillError("pending-dir-mode")
        try:
            _validate_storage_directory(self.root)
        except (OSError, ValueError) as error:
            raise PendingDistillError("pending-dir-unsafe") from error

    @staticmethod
    def _validate_fd(descriptor: int, *, kind: str) -> os.stat_result:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise PendingDistillError(f"{kind}-not-regular")
        if metadata.st_nlink != 1:
            raise PendingDistillError(f"{kind}-multiple-links")
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise PendingDistillError(f"{kind}-owner")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise PendingDistillError(f"{kind}-mode")
        return metadata

    @staticmethod
    def _validate_path_matches_fd(
        path: Path, metadata: os.stat_result, *, kind: str
    ) -> None:
        try:
            path_metadata = path.lstat()
        except OSError as error:
            raise PendingDistillError(f"{kind}-path-changed") from error
        if stat.S_ISLNK(path_metadata.st_mode) or (
            path_metadata.st_dev,
            path_metadata.st_ino,
        ) != (metadata.st_dev, metadata.st_ino):
            raise PendingDistillError(f"{kind}-path-changed")

    @contextmanager
    def _open_lock(
        self,
        path: Path,
        *,
        nonblocking: bool,
        lock: bool = True,
    ) -> Iterator[int]:
        if path.is_symlink():
            raise PendingDistillError("lock-symlink")
        existed = path.exists()
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags, 0o600)
        except OSError as error:
            raise PendingDistillError("lock-open-failed") from error
        try:
            if not existed:
                os.fchmod(descriptor, 0o600)
            metadata = self._validate_fd(descriptor, kind="lock")
            self._validate_path_matches_fd(path, metadata, kind="lock")
            if lock:
                operation = fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0)
                try:
                    fcntl.flock(descriptor, operation)
                except BlockingIOError:
                    raise PendingDistillError("job-lock-held") from None
            yield descriptor
        finally:
            if lock:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                except OSError:
                    pass
            os.close(descriptor)

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        self._validate_root()
        with self._open_lock(self._lock_path, nonblocking=False):
            yield

    def job_path(self, job_id: str) -> Path:
        if not _SHA256_RE.fullmatch(job_id):
            raise PendingDistillError("job-id-invalid")
        return self.root / f"{job_id}.json"

    def _read_path(self, path: Path) -> tuple[PendingDistillJob, os.stat_result]:
        if path.parent != self.root or path.suffix != ".json":
            raise PendingDistillError("job-path-invalid")
        if path.is_symlink():
            raise PendingDistillError("job-symlink")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError as error:
            raise PendingDistillError("job-missing") from error
        except OSError as error:
            raise PendingDistillError("job-open-failed") from error
        try:
            metadata = self._validate_fd(descriptor, kind="job")
            self._validate_path_matches_fd(path, metadata, kind="job")
            payload = bytearray()
            while len(payload) <= _MAX_JOB_BYTES:
                chunk = os.read(descriptor, min(65536, _MAX_JOB_BYTES + 1 - len(payload)))
                if not chunk:
                    break
                payload.extend(chunk)
            if len(payload) > _MAX_JOB_BYTES:
                raise PendingDistillError("job-too-large")
        finally:
            os.close(descriptor)
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise PendingDistillError("job-json-invalid") from error
        if not isinstance(value, dict):
            raise PendingDistillError("job-json-not-object")
        job = PendingDistillJob.from_dict(value)
        if path.name != f"{job.job_id}.json":
            raise PendingDistillError("job-path-id-mismatch")
        return job, metadata

    def get(self, job_id: str) -> PendingDistillJob:
        with self._exclusive():
            return self._read_path(self.job_path(job_id))[0]

    def enqueue_once(
        self,
        *,
        session_id: str,
        transcript_path: Path,
        source_cwd: str,
        source_project: str,
        trigger: str,
        dryrun: int,
        isolation_profile: str,
        wiki_memory_enabled: str,
        memory_audience_scoped: str,
        memory_audience: str,
        memory_scope: str,
        honcho_memory_enabled: str,
        memory_user_label: str,
        memory_assistant_label: str,
        created_at: str | None = None,
    ) -> tuple[PendingDistillJob, bool]:
        self.initialize()
        transcript_hash = _hash_transcript(transcript_path)
        job_id = pending_distill_job_id(session_id, transcript_hash)
        job = PendingDistillJob(
            job_id=job_id,
            transcript_sha256=transcript_hash,
            session_id=session_id,
            transcript_path=os.fspath(transcript_path),
            source_cwd=source_cwd,
            source_project=source_project,
            trigger=trigger,
            dryrun=dryrun,
            created_at=created_at or _timestamp(),
            isolation_profile=isolation_profile,
            wiki_memory_enabled=wiki_memory_enabled,
            memory_audience_scoped=memory_audience_scoped,
            memory_audience=memory_audience,
            memory_scope=memory_scope,
            honcho_memory_enabled=honcho_memory_enabled,
            memory_user_label=memory_user_label,
            memory_assistant_label=memory_assistant_label,
        )
        path = self.job_path(job_id)
        with self._exclusive():
            if path.exists() or path.is_symlink():
                return self._read_path(path)[0], False
            payload = (
                json.dumps(
                    job.to_dict(),
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
            _atomic_write_bytes(path, payload)
            self._read_path(path)
        return job, True

    def scan(self, *, limit: int) -> tuple[tuple[Path, ...], tuple[str, ...]]:
        if limit <= 0:
            return (), ()
        self.validate_existing()
        ready: list[Path] = []
        rejected: list[str] = []
        with self._exclusive():
            for path in sorted(self.root.iterdir(), key=lambda item: item.name):
                if path.name == self._lock_path.name or path.name.endswith(".lock"):
                    continue
                if path.suffix != ".json" or not _SHA256_RE.fullmatch(path.stem):
                    rejected.append("unexpected-entry")
                    continue
                try:
                    self._read_path(path)
                except PendingDistillError as error:
                    rejected.append(error.code)
                    continue
                if len(ready) < limit:
                    ready.append(path)
        return tuple(ready), tuple(rejected)

    @staticmethod
    def _job_environment(job: PendingDistillJob, base: Mapping[str, str]) -> dict[str, str]:
        scoped = job.memory_audience_scoped not in _DISABLED_VALUES
        if scoped and job.memory_audience not in {"private", "shared"}:
            raise PendingDistillError("invalid-memory-audience")
        configured_scope = base.get("CCC_MEMORY_SCOPE", "")
        if scoped and configured_scope and job.memory_scope and configured_scope != job.memory_scope:
            raise PendingDistillError("memory-scope-mismatch")
        environment = dict(base)
        environment.update(
            {
                "CLAUDE_DISTILL_CLAIMED": "1",
                "CLAUDE_DISTILL_TRIGGER": job.trigger,
                "CLAUDE_DISTILL_SESSION": job.session_id,
                "CLAUDE_DISTILL_TRANSCRIPT": job.transcript_path,
                "CLAUDE_DISTILL_SOURCE_CWD": job.source_cwd,
                "CLAUDE_DISTILL_SOURCE_PROJECT": job.source_project,
                "CLAUDE_DISTILL_DRYRUN": str(job.dryrun),
                "CCC_NODE_ISOLATION_PROFILE": job.isolation_profile,
                "CCC_WIKI_MEMORY_ENABLED": job.wiki_memory_enabled,
                "CCC_HONCHO_MEMORY_ENABLED": job.honcho_memory_enabled,
                "CCC_MEMORY_USER_LABEL": job.memory_user_label,
                "CCC_MEMORY_ASSISTANT_LABEL": job.memory_assistant_label,
            }
        )
        if scoped:
            environment.update(
                {
                    "CCC_MEMORY_AUDIENCE_SCOPED": job.memory_audience_scoped,
                    "CCC_MEMORY_AUDIENCE": job.memory_audience,
                    "CCC_MEMORY_SCOPE": job.memory_scope,
                }
            )
        return environment

    def _complete(
        self,
        path: Path,
        expected_job: PendingDistillJob,
        expected_metadata: os.stat_result,
        lock_path: Path,
    ) -> None:
        with self._exclusive():
            current_job, current_metadata = self._read_path(path)
            if current_job != expected_job or (
                current_metadata.st_dev,
                current_metadata.st_ino,
            ) != (expected_metadata.st_dev, expected_metadata.st_ino):
                raise PendingDistillError("job-changed-during-claim")
            path.unlink()
            _fsync_directory(self.root)
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
            _fsync_directory(self.root)

    def claim_and_run(
        self,
        job_path: Path,
        command: Sequence[str],
        *,
        environment: Mapping[str, str] | None = None,
        success_code: int = 0,
    ) -> PendingDistillClaimResult:
        if not command:
            raise ValueError("claim command must not be empty")
        if success_code < 0 or success_code > 255:
            raise ValueError("success_code must be an exit status")
        self.validate_existing()
        path = Path(os.path.abspath(os.fspath(job_path)))
        if path.parent != self.root or not _SHA256_RE.fullmatch(path.stem) or path.suffix != ".json":
            raise PendingDistillError("job-path-invalid")
        # Preserve the historical ``<job>.json.lock`` location.  Lock files are
        # ephemeral, but keeping the name avoids surprising existing probes.
        lock_path = self.root / f"{path.name}.lock"
        try:
            lock_context = self._open_lock(lock_path, nonblocking=True)
            with lock_context as lock_descriptor:
                job, metadata = self._read_path(path)
                _validate_transcript_reference(Path(job.transcript_path))
                child_env = self._job_environment(job, environment or os.environ)
                result = subprocess.run(
                    list(command),
                    check=False,
                    env=child_env,
                    pass_fds=(lock_descriptor,),
                )
                if result.returncode != success_code:
                    return PendingDistillClaimResult(
                        "retained", job.job_id, result.returncode
                    )
                self._complete(path, job, metadata, lock_path)
                return PendingDistillClaimResult(
                    "completed", job.job_id, result.returncode
                )
        except PendingDistillError as error:
            if error.code == "job-lock-held":
                return PendingDistillClaimResult("held", path.stem)
            raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage durable pending distill jobs")
    subparsers = parser.add_subparsers(dest="command", required=True)

    enqueue = subparsers.add_parser("enqueue")
    enqueue.add_argument("--queue-dir", type=Path, required=True)
    enqueue.add_argument("--session-id", required=True)
    enqueue.add_argument("--transcript-path", type=Path, required=True)
    enqueue.add_argument("--source-cwd", default="")
    enqueue.add_argument("--source-project", default="")
    enqueue.add_argument("--trigger", default="manual")
    enqueue.add_argument("--dryrun", type=int, choices=(0, 1), default=0)
    enqueue.add_argument("--isolation-profile", default="fleet")
    enqueue.add_argument("--wiki-memory-enabled", default="1")
    enqueue.add_argument("--memory-audience-scoped", default="0")
    enqueue.add_argument("--memory-audience", default="legacy")
    enqueue.add_argument("--memory-scope", default="")
    enqueue.add_argument("--honcho-memory-enabled", default="1")
    enqueue.add_argument("--memory-user-label", default="Seo Jin On / 서진원")
    enqueue.add_argument(
        "--memory-assistant-label", default="dungae, a Hermes Team2 worker"
    )

    scan = subparsers.add_parser("scan")
    scan.add_argument("--queue-dir", type=Path, required=True)
    scan.add_argument("--limit", type=int, required=True)

    run = subparsers.add_parser("run")
    run.add_argument("--queue-dir", type=Path, required=True)
    run.add_argument("--job-path", type=Path, required=True)
    run.add_argument("--success-code", type=int, default=0)
    run.add_argument("child", nargs=argparse.REMAINDER)
    return parser


def _json_result(value: Mapping[str, object]) -> None:
    print(json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")))


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        journal = PendingDistillJournal(arguments.queue_dir)
        if arguments.command == "enqueue":
            job, created = journal.enqueue_once(
                session_id=arguments.session_id,
                transcript_path=arguments.transcript_path,
                source_cwd=arguments.source_cwd,
                source_project=arguments.source_project,
                trigger=arguments.trigger,
                dryrun=arguments.dryrun,
                isolation_profile=arguments.isolation_profile,
                wiki_memory_enabled=arguments.wiki_memory_enabled,
                memory_audience_scoped=arguments.memory_audience_scoped,
                memory_audience=arguments.memory_audience,
                memory_scope=arguments.memory_scope,
                honcho_memory_enabled=arguments.honcho_memory_enabled,
                memory_user_label=arguments.memory_user_label,
                memory_assistant_label=arguments.memory_assistant_label,
            )
            _json_result(
                {
                    "status": "enqueued" if created else "dedup",
                    "job_id": job.job_id,
                    "path": os.fspath(journal.job_path(job.job_id)),
                }
            )
            return 0
        if arguments.command == "scan":
            ready, rejected = journal.scan(limit=arguments.limit)
            for code in rejected:
                print(f"pending rejected reason={code}", file=sys.stderr)
            for path in ready:
                sys.stdout.buffer.write(os.fsencode(path) + b"\0")
            return 0
        child = list(arguments.child)
        if child and child[0] == "--":
            child = child[1:]
        result = journal.claim_and_run(
            arguments.job_path, child, success_code=arguments.success_code
        )
        _json_result(
            {
                "status": result.status,
                "job_id": result.job_id,
                "child_returncode": result.child_returncode,
            }
        )
        return 0 if result.status in {"completed", "held"} else 1
    except PendingDistillError as error:
        print(f"pending rejected reason={error.code}", file=sys.stderr)
        _json_result({"status": "rejected", "reason": error.code})
        return 2
    except (OSError, ValueError, subprocess.SubprocessError):
        print("pending rejected reason=boundary-failed", file=sys.stderr)
        _json_result({"status": "rejected", "reason": "boundary-failed"})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
