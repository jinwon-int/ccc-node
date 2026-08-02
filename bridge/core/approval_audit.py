"""Owner-only, bounded and body-free approval decision audit (#870)."""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
import argparse
import fcntl
from hashlib import sha256
import json
import logging
import os
from pathlib import Path
import re
import stat
import threading
from typing import Iterator, Literal, Sequence

from telegram_bot.utils.secure_fs import _atomic_write_bytes, ensure_private_directory

logger = logging.getLogger(__name__)

_MAX_RECORD_BYTES = 4 * 1024
_DEFAULT_MAX_RECORDS = 4000
_SAFE_LABEL = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,95}$")
_FINGERPRINT = re.compile(r"^hmac-sha256:[0-9a-f]{64}$")
_DISPLAY_FINGERPRINT = re.compile(r"^sha256:[0-9a-f]{64}$")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
_DECISIONS = frozenset({"allow", "deny", "timeout", "invalidated"})
_REASONS = frozenset(
    {
        "owner_allow",
        "owner_deny",
        "timeout",
        "expired",
        "stale_generation",
        "fingerprint_mismatch",
        "shutdown",
        "send_failure",
        "cancelled",
    }
)

ApprovalAuditEvent = Literal["asked", "answered"]
ApprovalAuditDecision = Literal["allow", "deny", "timeout", "invalidated"]
ApprovalAuditReason = Literal[
    "owner_allow",
    "owner_deny",
    "timeout",
    "expired",
    "stale_generation",
    "fingerprint_mismatch",
    "shutdown",
    "send_failure",
    "cancelled",
]


@dataclass(frozen=True, slots=True)
class ApprovalAuditRecord:
    event: ApprovalAuditEvent
    approval_ref: str
    provider: str
    action: str
    target_shape: str
    session_ref: str
    turn_ref: str
    request_ref: str
    actor_ref: str | None
    request_fingerprint: str
    display_fingerprint: str
    asked_at: str
    answered_at: str | None = None
    decision: ApprovalAuditDecision | None = None
    reason: ApprovalAuditReason | None = None
    latency_ms: int | None = None
    redaction_flags: tuple[str, ...] = ()
    displayed_fields: tuple[str, ...] = ()
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("unsupported approval audit schema")
        if self.event not in {"asked", "answered"}:
            raise ValueError("unsupported approval audit event")
        self._validate_references()
        self._validate_labels()
        self._validate_phase()

    def _validate_references(self) -> None:
        for value in (
            self.approval_ref,
            self.session_ref,
            self.turn_ref,
            self.request_ref,
            self.request_fingerprint,
        ):
            if not _FINGERPRINT.fullmatch(value):
                raise ValueError("approval audit references must be keyed SHA-256 values")
        if not _DISPLAY_FINGERPRINT.fullmatch(self.display_fingerprint):
            raise ValueError("approval display fingerprint must be SHA-256")
        if not _TIMESTAMP.fullmatch(self.asked_at):
            raise ValueError("approval asked timestamp must be normalized UTC")
        if self.answered_at is not None and not _TIMESTAMP.fullmatch(self.answered_at):
            raise ValueError("approval answer timestamp must be normalized UTC")

    def _validate_labels(self) -> None:
        if self.actor_ref is not None and not _FINGERPRINT.fullmatch(self.actor_ref):
            raise ValueError("approval actor reference must be opaque")
        for label in (
            self.provider,
            self.action,
            self.target_shape,
            *self.redaction_flags,
            *self.displayed_fields,
        ):
            if not _SAFE_LABEL.fullmatch(label):
                raise ValueError("approval audit labels must be body-free")

    def _validate_phase(self) -> None:
        if self.event == "asked":
            if any(
                value is not None
                for value in (self.answered_at, self.decision, self.reason, self.latency_ms)
            ):
                raise ValueError("asked records cannot contain terminal fields")
        elif None in (self.answered_at, self.decision, self.reason, self.latency_ms):
            raise ValueError("answered records require terminal fields")
        if self.decision is not None and self.decision not in _DECISIONS:
            raise ValueError("unsupported approval audit decision")
        if self.reason is not None and self.reason not in _REASONS:
            raise ValueError("unsupported approval audit reason")
        if self.latency_ms is not None and self.latency_ms < 0:
            raise ValueError("approval latency must be non-negative")

    def dedup_key(self) -> str:
        phase = "asked" if self.event == "asked" else "terminal"
        return sha256(f"{self.approval_ref}:{phase}".encode("utf-8")).hexdigest()

    def to_record(self) -> dict[str, object]:
        record: dict[str, object] = {
            "schema_version": self.schema_version,
            "event": self.event,
            "approval_ref": self.approval_ref,
            "provider": self.provider,
            "action": self.action,
            "target_shape": self.target_shape,
            "session_ref": self.session_ref,
            "turn_ref": self.turn_ref,
            "request_ref": self.request_ref,
            "request_fingerprint": self.request_fingerprint,
            "display_fingerprint": self.display_fingerprint,
            "asked_at": self.asked_at,
            "redaction_flags": list(self.redaction_flags),
            "displayed_fields": list(self.displayed_fields),
        }
        if self.actor_ref is not None:
            record["actor_ref"] = self.actor_ref
        if self.event == "answered":
            record.update(
                answered_at=self.answered_at,
                decision=self.decision,
                reason=self.reason,
                latency_ms=self.latency_ms,
            )
        return record


@dataclass(frozen=True, slots=True)
class ApprovalAuditWriteResult:
    written: bool
    deduped: bool = False
    reason: str | None = None


class ApprovalAuditLedger:
    """Strict owner-only JSONL ledger with asked/terminal phase deduplication."""

    def __init__(self, directory: Path, *, max_records: int = _DEFAULT_MAX_RECORDS) -> None:
        self.directory = Path(os.path.abspath(os.fspath(directory)))
        self.path = self.directory / "approval-audit.jsonl"
        self._lock_path = self.directory / ".approval-audit.lock"
        self._max_records = max(2, int(max_records))
        self._thread_lock = threading.RLock()

    @staticmethod
    def _validate_private_directory(path: Path) -> None:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise PermissionError("approval audit directory must be a real directory")
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise PermissionError("approval audit directory has the wrong owner")
        if stat.S_IMODE(metadata.st_mode) != 0o700:
            raise PermissionError("approval audit directory must use mode 0700")

    @staticmethod
    def _validate_private_file(path: Path) -> None:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise PermissionError("approval audit state must be a regular file")
        if metadata.st_nlink != 1:
            raise PermissionError("approval audit state must not have hard links")
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise PermissionError("approval audit state has the wrong owner")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise PermissionError("approval audit state must use mode 0600")

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        ensure_private_directory(self.directory)
        self._validate_private_directory(self.directory)
        with self._thread_lock:
            self._validate_private_file(self._lock_path)
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(self._lock_path, flags, 0o600)
            try:
                metadata = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_nlink != 1
                    or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
                    or stat.S_IMODE(metadata.st_mode) != 0o600
                ):
                    raise PermissionError("approval audit lock is unsafe")
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                yield
            finally:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)

    def _read_lines(self) -> list[str]:
        self._validate_private_file(self.path)
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        return [line for line in text.splitlines() if line.strip()]

    def record(self, record: ApprovalAuditRecord) -> ApprovalAuditWriteResult:
        """Append one phase exactly once; failures remain body-free and fail-open."""
        try:
            key = record.dedup_key()
            payload = json.dumps(
                {**record.to_record(), "dedup": key},
                ensure_ascii=False,
                sort_keys=True,
            )
            if len(payload.encode("utf-8")) > _MAX_RECORD_BYTES:
                return ApprovalAuditWriteResult(False, reason="oversize")
            with self._exclusive():
                lines = self._read_lines()
                for line in lines:
                    try:
                        prior = json.loads(line)
                        if isinstance(prior, dict) and prior.get("dedup") == key:
                            return ApprovalAuditWriteResult(False, deduped=True)
                    except (TypeError, ValueError):
                        continue
                lines.append(payload)
                if len(lines) > self._max_records:
                    lines = lines[-self._max_records :]
                _atomic_write_bytes(self.path, ("\n".join(lines) + "\n").encode("utf-8"))
            return ApprovalAuditWriteResult(True)
        except Exception as error:
            logger.warning("approval audit write failed (continuing): %s", type(error).__name__)
            return ApprovalAuditWriteResult(False, reason="write-error")

    def summarize(self) -> dict[str, object]:
        """Return body-free policy metrics while tolerating malformed prior rows."""
        try:
            with self._exclusive():
                lines = self._read_lines()
            by_action: Counter[str] = Counter()
            by_decision: Counter[str] = Counter()
            by_reason: Counter[str] = Counter()
            latencies: list[int] = []
            malformed = 0
            for line in lines:
                try:
                    record = json.loads(line)
                except (TypeError, ValueError):
                    malformed += 1
                    continue
                if not isinstance(record, dict):
                    malformed += 1
                    continue
                if record.get("event") != "answered":
                    continue
                action = record.get("action")
                decision = record.get("decision")
                reason = record.get("reason")
                latency = record.get("latency_ms")
                if isinstance(action, str) and _SAFE_LABEL.fullmatch(action):
                    by_action[action] += 1
                if isinstance(decision, str) and _SAFE_LABEL.fullmatch(decision):
                    by_decision[decision] += 1
                if isinstance(reason, str) and _SAFE_LABEL.fullmatch(reason):
                    by_reason[reason] += 1
                if isinstance(latency, int) and latency >= 0:
                    latencies.append(latency)
            return {
                "ok": True,
                "schema_version": 1,
                "terminal_records": sum(by_decision.values()),
                "by_action": dict(sorted(by_action.items())),
                "by_decision": dict(sorted(by_decision.items())),
                "by_reason": dict(sorted(by_reason.items())),
                "latency_ms": {
                    "count": len(latencies),
                    "average": round(sum(latencies) / len(latencies), 2) if latencies else None,
                    "maximum": max(latencies) if latencies else None,
                },
                "malformed_records": malformed,
            }
        except Exception as error:
            logger.warning("approval audit summary failed: %s", type(error).__name__)
            return {"ok": False, "schema_version": 1, "reason": "read-error"}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Print body-free ccc-node approval audit metrics."
    )
    parser.add_argument(
        "--directory",
        type=Path,
        required=True,
        help="Approval audit directory (for example BOT_DATA_DIR/approval-audit).",
    )
    parser.add_argument("--json", action="store_true", help="Emit one JSON object.")
    args = parser.parse_args(argv)
    summary = ApprovalAuditLedger(args.directory).summarize()
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    else:
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if summary.get("ok") is True else 2


__all__ = [
    "ApprovalAuditDecision",
    "ApprovalAuditEvent",
    "ApprovalAuditLedger",
    "ApprovalAuditReason",
    "ApprovalAuditRecord",
    "ApprovalAuditWriteResult",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
