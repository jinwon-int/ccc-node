"""Strict data contracts for provider-neutral distill snapshot jobs."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import re
from typing import Any, Literal, Mapping

DISTILL_SCHEMA_VERSION = 1
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class DistillTrigger(str, Enum):
    NEW_COMMAND = "new_command"
    PROVIDER_SWITCH = "provider_switch"
    AUTO_NEW = "auto_new"


class DistillJobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING_SNAPSHOT = "running_snapshot"
    SNAPSHOT_DONE = "snapshot_done"
    RETRYABLE_FAILED = "retryable_failed"
    TERMINAL_FAILED = "terminal_failed"


@dataclass(frozen=True, slots=True)
class TranscriptBounds:
    max_turns: int = 20
    max_items: int = 200
    max_messages: int = 50
    max_bytes: int = 64 * 1024
    max_message_bytes: int = 8 * 1024
    max_age_seconds: int = 7 * 24 * 60 * 60

    def __post_init__(self) -> None:
        for name in (
            "max_turns",
            "max_items",
            "max_messages",
            "max_bytes",
            "max_message_bytes",
            "max_age_seconds",
        ):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class TranscriptMessage:
    role: Literal["user", "assistant"]
    text: str
    timestamp: str | None

    def __post_init__(self) -> None:
        if self.role not in {"user", "assistant"}:
            raise ValueError("transcript role must be user or assistant")
        if not isinstance(self.text, str) or not self.text:
            raise ValueError("transcript text must not be empty")
        if self.timestamp is not None and (
            not isinstance(self.timestamp, str) or not self.timestamp
        ):
            raise ValueError("transcript timestamp must be a non-empty string or null")

    def to_dict(self) -> dict[str, Any]:
        return {"role": self.role, "text": self.text, "timestamp": self.timestamp}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> TranscriptMessage:
        role = value.get("role")
        if role not in {"user", "assistant"}:
            raise ValueError("invalid transcript role")
        text = value.get("text")
        timestamp = value.get("timestamp")
        if not isinstance(text, str):
            raise ValueError("invalid transcript text")
        if timestamp is not None and not isinstance(timestamp, str):
            raise ValueError("invalid transcript timestamp")
        return cls(role, text, timestamp)


@dataclass(frozen=True, slots=True)
class CodexTranscriptSnapshot:
    thread_hash: str
    last_turn_id: str | None
    messages: tuple[TranscriptMessage, ...]
    byte_count: int
    truncated: bool
    captured_at: str

    def __post_init__(self) -> None:
        if not _SHA256_RE.fullmatch(self.thread_hash):
            raise ValueError("thread_hash must be a SHA-256 hex digest")
        if self.last_turn_id is not None and not self.last_turn_id:
            raise ValueError("last_turn_id must be non-empty or null")
        if type(self.byte_count) is not int or self.byte_count < 0:
            raise ValueError("byte_count must be a non-negative integer")
        actual_bytes = sum(len(message.text.encode("utf-8")) for message in self.messages)
        if self.byte_count != actual_bytes:
            raise ValueError("byte_count does not match transcript messages")
        if type(self.truncated) is not bool:
            raise ValueError("truncated must be boolean")
        if not self.captured_at:
            raise ValueError("captured_at must not be empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_hash": self.thread_hash,
            "last_turn_id": self.last_turn_id,
            "messages": [message.to_dict() for message in self.messages],
            "byte_count": self.byte_count,
            "truncated": self.truncated,
            "captured_at": self.captured_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CodexTranscriptSnapshot:
        raw_messages = value.get("messages")
        if not isinstance(raw_messages, list):
            raise ValueError("snapshot messages must be a list")
        if any(not isinstance(item, Mapping) for item in raw_messages):
            raise ValueError("snapshot messages must contain objects only")
        messages = tuple(
            TranscriptMessage.from_dict(item)
            for item in raw_messages
            if isinstance(item, Mapping)
        )
        thread_hash = value.get("thread_hash")
        last_turn_id = value.get("last_turn_id")
        byte_count = value.get("byte_count")
        truncated = value.get("truncated")
        captured_at = value.get("captured_at")
        if not isinstance(thread_hash, str):
            raise ValueError("invalid snapshot thread_hash")
        if last_turn_id is not None and not isinstance(last_turn_id, str):
            raise ValueError("invalid snapshot last_turn_id")
        if type(byte_count) is not int or type(truncated) is not bool:
            raise ValueError("invalid snapshot counters")
        if not isinstance(captured_at, str):
            raise ValueError("invalid snapshot captured_at")
        return cls(
            thread_hash,
            last_turn_id,
            messages,
            byte_count,
            truncated,
            captured_at,
        )


@dataclass(frozen=True, slots=True)
class DistillJob:
    job_id: str
    provider: str
    thread_id: str
    thread_hash: str
    discriminator: str
    trigger: DistillTrigger
    status: DistillJobStatus
    schema_version: int
    created_at: str
    updated_at: str
    attempts: int = 0
    lease_epoch: int = 0
    owner_token: str | None = None
    lease_expires_at: str | None = None
    snapshot: CodexTranscriptSnapshot | None = None
    error_code: str | None = None

    def __post_init__(self) -> None:
        if not _SHA256_RE.fullmatch(self.job_id):
            raise ValueError("job_id must be a SHA-256 hex digest")
        if self.provider != "codex":
            raise ValueError("distill jobs support the Codex provider only")
        if not isinstance(self.thread_id, str) or not self.thread_id:
            raise ValueError("invalid distill job thread identity")
        expected_thread_hash = hashlib.sha256(self.thread_id.encode("utf-8")).hexdigest()
        if (
            not _SHA256_RE.fullmatch(self.thread_hash)
            or self.thread_hash != expected_thread_hash
        ):
            raise ValueError("invalid distill job thread identity")
        if not self.discriminator:
            raise ValueError("distill job discriminator must not be empty")
        if self.schema_version <= 0 or self.attempts < 0 or self.lease_epoch < 0:
            raise ValueError("invalid distill job counters")
        if self.error_code is not None and not _SAFE_ERROR_CODE_RE.fullmatch(
            self.error_code
        ):
            raise ValueError("invalid distill job error_code")

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "provider": self.provider,
            "thread_id": self.thread_id,
            "thread_hash": self.thread_hash,
            "discriminator": self.discriminator,
            "trigger": self.trigger.value,
            "status": self.status.value,
            "schema_version": self.schema_version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "attempts": self.attempts,
            "lease_epoch": self.lease_epoch,
            "owner_token": self.owner_token,
            "lease_expires_at": self.lease_expires_at,
            "snapshot": self.snapshot.to_dict() if self.snapshot is not None else None,
            "error_code": self.error_code,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> DistillJob:
        required_strings = (
            "job_id",
            "provider",
            "thread_id",
            "thread_hash",
            "discriminator",
            "created_at",
            "updated_at",
        )
        for name in required_strings:
            if not isinstance(value.get(name), str) or not value[name]:
                raise ValueError(f"invalid distill job field: {name}")
        snapshot_value = value.get("snapshot")
        if snapshot_value is not None and not isinstance(snapshot_value, Mapping):
            raise ValueError("invalid distill job snapshot")
        attempts = value.get("attempts", 0)
        lease_epoch = value.get("lease_epoch", 0)
        schema_version = value.get("schema_version")
        if type(attempts) is not int or attempts < 0:
            raise ValueError("invalid distill job attempts")
        if type(lease_epoch) is not int or lease_epoch < 0:
            raise ValueError("invalid distill job lease_epoch")
        if type(schema_version) is not int or schema_version <= 0:
            raise ValueError("invalid distill job schema_version")
        owner_token = value.get("owner_token")
        lease_expires_at = value.get("lease_expires_at")
        error_code = value.get("error_code")
        for name, optional in (
            ("owner_token", owner_token),
            ("lease_expires_at", lease_expires_at),
            ("error_code", error_code),
        ):
            if optional is not None and not isinstance(optional, str):
                raise ValueError(f"invalid distill job field: {name}")
        return cls(
            job_id=value["job_id"],
            provider=value["provider"],
            thread_id=value["thread_id"],
            thread_hash=value["thread_hash"],
            discriminator=value["discriminator"],
            trigger=DistillTrigger(value.get("trigger")),
            status=DistillJobStatus(value.get("status")),
            schema_version=schema_version,
            created_at=value["created_at"],
            updated_at=value["updated_at"],
            attempts=attempts,
            lease_epoch=lease_epoch,
            owner_token=owner_token,
            lease_expires_at=lease_expires_at,
            snapshot=(
                CodexTranscriptSnapshot.from_dict(snapshot_value)
                if snapshot_value is not None
                else None
            ),
            error_code=error_code,
        )
