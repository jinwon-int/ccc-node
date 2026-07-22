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
_PRIVATE_MEMORY_SCOPE_RE = re.compile(r"^private-[0-9a-f]{32}$")
_DISTILL_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class DistillTrigger(str, Enum):
    NEW_COMMAND = "new_command"
    PROVIDER_SWITCH = "provider_switch"
    AUTO_NEW = "auto_new"
    EXPLICIT = "explicit"
    SHUTDOWN = "shutdown"
    CHECKPOINT = "checkpoint"


class DistillJobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING_SNAPSHOT = "running_snapshot"
    SNAPSHOT_DONE = "snapshot_done"
    RETRYABLE_FAILED = "retryable_failed"
    TERMINAL_FAILED = "terminal_failed"
    RUNNING_EXTRACTION = "running_extraction"
    EXTRACTION_RETRYABLE_FAILED = "extraction_retryable_failed"
    EXTRACTION_DONE = "extraction_done"
    EXTRACTION_TERMINAL_FAILED = "extraction_terminal_failed"


class DistillLocalSinkStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    RETRYABLE_FAILED = "retryable_failed"
    DONE = "done"
    TERMINAL_FAILED = "terminal_failed"
    UNROUTABLE = "unroutable"


class DistillWikiSinkStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    RETRYABLE_FAILED = "retryable_failed"
    DONE = "done"
    TERMINAL_FAILED = "terminal_failed"
    DISABLED = "disabled"


def _parse_wiki_sink_fields(
    value: Mapping[str, Any],
    *,
    extraction_output: object,
) -> tuple[DistillWikiSinkStatus | None, int, int, str | None, str | None]:
    attempts = value.get("wiki_sink_attempts", 0)
    lease_epoch = value.get("wiki_sink_lease_epoch", 0)
    if type(attempts) is not int or attempts < 0:
        raise ValueError("invalid distill job wiki_sink_attempts")
    if type(lease_epoch) is not int or lease_epoch < 0:
        raise ValueError("invalid distill job wiki_sink_lease_epoch")
    owner_token = value.get("wiki_sink_owner_token")
    lease_expires_at = value.get("wiki_sink_lease_expires_at")
    if owner_token is not None and not isinstance(owner_token, str):
        raise ValueError("invalid distill job field: wiki_sink_owner_token")
    if lease_expires_at is not None and not isinstance(lease_expires_at, str):
        raise ValueError("invalid distill job field: wiki_sink_lease_expires_at")
    raw_status = value.get("wiki_sink_status")
    if raw_status is None:
        status = (
            DistillWikiSinkStatus.PENDING if extraction_output is not None else None
        )
    elif isinstance(raw_status, str):
        status = DistillWikiSinkStatus(raw_status)
    else:
        raise ValueError("invalid distill job field: wiki_sink_status")
    return status, attempts, lease_epoch, owner_token, lease_expires_at


def _validate_wiki_sink_lease(
    status: DistillWikiSinkStatus | None,
    owner_token: str | None,
    lease_expires_at: str | None,
) -> None:
    running = status is DistillWikiSinkStatus.RUNNING
    if running != (owner_token is not None and lease_expires_at is not None):
        raise ValueError("Wiki sink running state requires a complete lease")


@dataclass(frozen=True, slots=True)
class DistillExtractionAccounting:
    """Body-free accounting for one provider extraction attempt.

    ``estimated_max_tokens`` is the conservative pre-spend reservation used by
    the shared usage meter. It is deliberately not represented as actual
    provider token usage.
    """

    model: str
    snapshot_bytes: int
    duration_ms: int
    estimated_max_tokens: int

    def __post_init__(self) -> None:
        if not _DISTILL_MODEL_RE.fullmatch(self.model):
            raise ValueError("invalid distill extraction model")
        for name in ("snapshot_bytes", "duration_ms", "estimated_max_tokens"):
            value = getattr(self, name)
            if type(value) is not int or value < 0 or value > 10**12:
                raise ValueError(f"invalid distill extraction accounting: {name}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "snapshot_bytes": self.snapshot_bytes,
            "duration_ms": self.duration_ms,
            "estimated_max_tokens": self.estimated_max_tokens,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> DistillExtractionAccounting:
        model = value.get("model")
        snapshot_bytes = value.get("snapshot_bytes")
        duration_ms = value.get("duration_ms")
        estimated_max_tokens = value.get("estimated_max_tokens")
        if not isinstance(model, str):
            raise ValueError("invalid distill extraction accounting model")
        if (
            not isinstance(snapshot_bytes, int)
            or isinstance(snapshot_bytes, bool)
            or not isinstance(duration_ms, int)
            or isinstance(duration_ms, bool)
            or not isinstance(estimated_max_tokens, int)
            or isinstance(estimated_max_tokens, bool)
        ):
            raise ValueError("invalid distill extraction accounting counters")
        return cls(model, snapshot_bytes, duration_ms, estimated_max_tokens)

    @classmethod
    def parse_many(cls, value: object) -> tuple[DistillExtractionAccounting, ...]:
        if not isinstance(value, list) or any(
            not isinstance(item, Mapping) for item in value
        ):
            raise ValueError("invalid distill job extraction_accounting")
        return tuple(
            cls.from_dict(item) for item in value if isinstance(item, Mapping)
        )

    @staticmethod
    def validate_for_job(
        items: tuple[DistillExtractionAccounting, ...],
        *,
        attempts: int,
        snapshot: CodexTranscriptSnapshot | None,
    ) -> None:
        if len(items) > attempts:
            raise ValueError("distill accounting exceeds extraction attempts")
        if snapshot is not None and any(
            item.snapshot_bytes != snapshot.byte_count for item in items
        ):
            raise ValueError("distill accounting snapshot bytes do not match")


def validate_memory_route(audience: str | None, scope: str | None) -> None:
    if audience is None and scope is None:
        return
    valid = (audience == "shared" and scope == "shared") or (
        audience == "private"
        and isinstance(scope, str)
        and _PRIVATE_MEMORY_SCOPE_RE.fullmatch(scope) is not None
    )
    if not valid:
        raise ValueError("invalid distill memory audience route")


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
    extraction_attempts: int = 0
    extraction_lease_epoch: int = 0
    extraction_output: str | None = None
    extraction_output_hash: str | None = None
    extraction_accounting: tuple[DistillExtractionAccounting, ...] = ()
    memory_audience: str | None = None
    memory_scope: str | None = None
    local_sink_status: DistillLocalSinkStatus | None = None
    local_sink_attempts: int = 0
    local_sink_lease_epoch: int = 0
    wiki_sink_status: DistillWikiSinkStatus | None = None
    wiki_sink_attempts: int = 0
    wiki_sink_lease_epoch: int = 0
    wiki_sink_owner_token: str | None = None
    wiki_sink_lease_expires_at: str | None = None

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
        if (
            self.schema_version <= 0
            or self.attempts < 0
            or self.lease_epoch < 0
            or self.extraction_attempts < 0
            or self.extraction_lease_epoch < 0
            or self.local_sink_attempts < 0
            or self.local_sink_lease_epoch < 0
            or self.wiki_sink_attempts < 0
            or self.wiki_sink_lease_epoch < 0
        ):
            raise ValueError("invalid distill job counters")
        validate_memory_route(self.memory_audience, self.memory_scope)
        if self.local_sink_status is DistillLocalSinkStatus.UNROUTABLE:
            if self.memory_audience is not None or self.memory_scope is not None:
                raise ValueError("unroutable local sink must not carry a route")
        elif self.local_sink_status is not None and (
            self.memory_audience is None or self.memory_scope is None
        ):
            raise ValueError("routable local sink status requires a memory route")
        _validate_wiki_sink_lease(
            self.wiki_sink_status,
            self.wiki_sink_owner_token,
            self.wiki_sink_lease_expires_at,
        )
        if self.error_code is not None and not _SAFE_ERROR_CODE_RE.fullmatch(
            self.error_code
        ):
            raise ValueError("invalid distill job error_code")
        if (self.extraction_output is None) != (self.extraction_output_hash is None):
            raise ValueError("distill extraction output and hash must be stored together")
        if self.extraction_output is not None:
            if not self.extraction_output:
                raise ValueError("distill extraction output must not be empty")
            expected_hash = hashlib.sha256(
                self.extraction_output.encode("utf-8")
            ).hexdigest()
            if (
                not isinstance(self.extraction_output_hash, str)
                or not _SHA256_RE.fullmatch(self.extraction_output_hash)
                or self.extraction_output_hash != expected_hash
            ):
                raise ValueError("invalid distill extraction output hash")
        DistillExtractionAccounting.validate_for_job(
            self.extraction_accounting,
            attempts=self.extraction_attempts,
            snapshot=self.snapshot,
        )

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
            "extraction_attempts": self.extraction_attempts,
            "extraction_lease_epoch": self.extraction_lease_epoch,
            "extraction_output": self.extraction_output,
            "extraction_output_hash": self.extraction_output_hash,
            "extraction_accounting": [
                item.to_dict() for item in self.extraction_accounting
            ],
            "memory_audience": self.memory_audience,
            "memory_scope": self.memory_scope,
            "local_sink_status": (
                self.local_sink_status.value
                if self.local_sink_status is not None
                else None
            ),
            "local_sink_attempts": self.local_sink_attempts,
            "local_sink_lease_epoch": self.local_sink_lease_epoch,
            "wiki_sink_status": (
                self.wiki_sink_status.value
                if self.wiki_sink_status is not None
                else None
            ),
            "wiki_sink_attempts": self.wiki_sink_attempts,
            "wiki_sink_lease_epoch": self.wiki_sink_lease_epoch,
            "wiki_sink_owner_token": self.wiki_sink_owner_token,
            "wiki_sink_lease_expires_at": self.wiki_sink_lease_expires_at,
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
        extraction_attempts = value.get("extraction_attempts", 0)
        extraction_lease_epoch = value.get("extraction_lease_epoch", 0)
        local_sink_attempts = value.get("local_sink_attempts", 0)
        local_sink_lease_epoch = value.get("local_sink_lease_epoch", 0)
        schema_version = value.get("schema_version")
        if type(attempts) is not int or attempts < 0:
            raise ValueError("invalid distill job attempts")
        if type(lease_epoch) is not int or lease_epoch < 0:
            raise ValueError("invalid distill job lease_epoch")
        if type(extraction_attempts) is not int or extraction_attempts < 0:
            raise ValueError("invalid distill job extraction_attempts")
        if type(extraction_lease_epoch) is not int or extraction_lease_epoch < 0:
            raise ValueError("invalid distill job extraction_lease_epoch")
        if type(local_sink_attempts) is not int or local_sink_attempts < 0:
            raise ValueError("invalid distill job local_sink_attempts")
        if type(local_sink_lease_epoch) is not int or local_sink_lease_epoch < 0:
            raise ValueError("invalid distill job local_sink_lease_epoch")
        if type(schema_version) is not int or schema_version <= 0:
            raise ValueError("invalid distill job schema_version")
        owner_token = value.get("owner_token")
        lease_expires_at = value.get("lease_expires_at")
        error_code = value.get("error_code")
        extraction_output = value.get("extraction_output")
        extraction_output_hash = value.get("extraction_output_hash")
        raw_extraction_accounting = value.get("extraction_accounting", [])
        memory_audience = value.get("memory_audience")
        memory_scope = value.get("memory_scope")
        for name, optional in (
            ("owner_token", owner_token),
            ("lease_expires_at", lease_expires_at),
            ("error_code", error_code),
            ("extraction_output", extraction_output),
            ("extraction_output_hash", extraction_output_hash),
            ("memory_audience", memory_audience),
            ("memory_scope", memory_scope),
        ):
            if optional is not None and not isinstance(optional, str):
                raise ValueError(f"invalid distill job field: {name}")
        extraction_accounting = DistillExtractionAccounting.parse_many(
            raw_extraction_accounting
        )
        raw_local_sink_status = value.get("local_sink_status")
        if raw_local_sink_status is None:
            local_sink_status = (
                DistillLocalSinkStatus.UNROUTABLE
                if extraction_output is not None and memory_audience is None
                else None
            )
        elif isinstance(raw_local_sink_status, str):
            local_sink_status = DistillLocalSinkStatus(raw_local_sink_status)
        else:
            raise ValueError("invalid distill job field: local_sink_status")
        (
            wiki_sink_status,
            wiki_sink_attempts,
            wiki_sink_lease_epoch,
            wiki_sink_owner_token,
            wiki_sink_lease_expires_at,
        ) = _parse_wiki_sink_fields(value, extraction_output=extraction_output)
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
            extraction_attempts=extraction_attempts,
            extraction_lease_epoch=extraction_lease_epoch,
            extraction_output=extraction_output,
            extraction_output_hash=extraction_output_hash,
            extraction_accounting=extraction_accounting,
            memory_audience=memory_audience,
            memory_scope=memory_scope,
            local_sink_status=local_sink_status,
            local_sink_attempts=local_sink_attempts,
            local_sink_lease_epoch=local_sink_lease_epoch,
            wiki_sink_status=wiki_sink_status,
            wiki_sink_attempts=wiki_sink_attempts,
            wiki_sink_lease_epoch=wiki_sink_lease_epoch,
            wiki_sink_owner_token=wiki_sink_owner_token,
            wiki_sink_lease_expires_at=wiki_sink_lease_expires_at,
        )
