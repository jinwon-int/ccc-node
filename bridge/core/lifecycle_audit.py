"""Owner-only, bounded, fail-open lifecycle audit ledger (#645).

Persists redacted, body-free ``LifecycleObservation`` records to a JSONL ledger
under an owner-only 0700 directory, deduped by the observation's ``dedup_key``
and bounded to the newest N records. Writing never raises into the caller's turn
path (fail-open) — a failure returns a body-free status and leaves a retryable
state, never a silent loss.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import json
import logging
import os
from pathlib import Path
import stat
import threading
from typing import Iterator

from telegram_bot.core.lifecycle_observation import (
    LifecycleObservation,
    normalize_agent_event,
)
from telegram_bot.utils.secure_fs import _atomic_write_bytes, ensure_private_directory

logger = logging.getLogger(__name__)

_MAX_RECORD_BYTES = 4 * 1024
_DEFAULT_MAX_RECORDS = 2000


@dataclass(frozen=True, slots=True)
class AuditWriteResult:
    written: bool
    deduped: bool = False
    reason: str | None = None  # body-free failure label when written is False


class LifecycleAuditLedger:
    def __init__(self, directory: Path, *, max_records: int = _DEFAULT_MAX_RECORDS) -> None:
        self.directory = Path(os.path.abspath(os.fspath(directory)))
        self.path = self.directory / "lifecycle-audit.jsonl"
        self._lock_path = self.directory / ".lifecycle-audit.lock"
        self._thread_lock = threading.RLock()
        self._max_records = max(1, int(max_records))

    @staticmethod
    def _validate_regular_file(path: Path) -> None:
        try:
            meta = path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(meta.st_mode) or not stat.S_ISREG(meta.st_mode):
            raise PermissionError(f"audit state must be regular: {path}")
        if meta.st_nlink != 1:
            raise PermissionError("audit state must not have hard links")
        if hasattr(os, "getuid") and meta.st_uid != os.getuid():
            raise PermissionError("audit state is not owned by this process")

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        ensure_private_directory(self.directory)
        with self._thread_lock:
            self._validate_regular_file(self._lock_path)
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(self._lock_path, flags, 0o600)
            try:
                os.fchmod(fd, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)

    def _read_lines(self) -> list[str]:
        self._validate_regular_file(self.path)
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        return [line for line in text.splitlines() if line.strip()]

    def record(self, observation: LifecycleObservation) -> AuditWriteResult:
        """Append one observation. Never raises (fail-open)."""
        try:
            key = observation.dedup_key()
            record = {**observation.to_record(), "dedup": key}
            payload = json.dumps(record, ensure_ascii=False, sort_keys=True)
            if len(payload.encode("utf-8")) > _MAX_RECORD_BYTES:
                return AuditWriteResult(False, reason="oversize")
            with self._exclusive():
                lines = self._read_lines()
                for line in lines:
                    try:
                        if json.loads(line).get("dedup") == key:
                            return AuditWriteResult(False, deduped=True)
                    except ValueError:
                        continue  # tolerate a malformed prior line
                lines.append(payload)
                if len(lines) > self._max_records:
                    lines = lines[-self._max_records:]  # bounded: keep newest
                blob = ("\n".join(lines) + "\n").encode("utf-8")
                _atomic_write_bytes(self.path, blob)
            return AuditWriteResult(True)
        except Exception as exc:  # fail-open: observability must never break a turn
            logger.warning("lifecycle audit write failed (continuing): %s", exc)
            return AuditWriteResult(False, reason="write-error")


class LifecycleObserver:
    """Fail-open tap that records live AgentEvents to the audit ledger (#645).

    Wired into the bridge event consume loop behind an opt-in flag. ``observe``
    normalizes an AgentEvent and records it; anything that goes wrong is
    swallowed so observability can never break a turn.
    """

    def __init__(self, ledger: LifecycleAuditLedger, *, provider: str) -> None:
        self._ledger = ledger
        self._provider = provider

    def observe(self, event: object, *, session_id: object) -> None:
        try:
            observation = normalize_agent_event(
                provider=self._provider, session_id=session_id, event=event
            )
            if observation is not None:
                self._ledger.record(observation)
        except Exception as exc:  # fail-open: never break the turn
            logger.warning("lifecycle observe failed (continuing): %s", exc)


def build_lifecycle_observer(config: object) -> "LifecycleObserver | None":
    """Build the opt-in observer, or None when disabled (#645).

    Returns None unless ``lifecycle_audit_enabled`` is set and the provider is
    supported, so a default node builds nothing.
    """
    if not getattr(config, "lifecycle_audit_enabled", False):
        return None
    provider = str(getattr(config, "agent_provider", "claude") or "claude")
    if provider not in ("claude", "codex"):
        return None
    base = getattr(config, "bot_data_dir", None) or (Path.home() / ".claude" / "state")
    ledger = LifecycleAuditLedger(Path(base) / "lifecycle-audit")
    return LifecycleObserver(ledger, provider=provider)


__all__ = [
    "AuditWriteResult",
    "LifecycleAuditLedger",
    "LifecycleObserver",
    "build_lifecycle_observer",
]
