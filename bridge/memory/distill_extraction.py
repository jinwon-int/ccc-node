"""Strict provider-neutral input/output boundary for Codex memory extraction.

This module is deliberately side-effect free. It normalizes and redacts an already
bounded Codex transcript snapshot, validates future provider output, and exposes a
backend protocol. Provider calls, journal transitions, and sink mutations belong to
later layers.
"""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import PurePosixPath
import re
from typing import Annotated, Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator

from .distill_types import CodexTranscriptSnapshot, DistillTrigger

DISTILL_EXTRACTION_SCHEMA_VERSION: Literal[1] = 1
MAX_EXTRACTION_JSON_BYTES = 64 * 1024
MAX_INPUT_MESSAGES = 50
MAX_INPUT_MESSAGE_BYTES = 8 * 1024
MAX_INPUT_BYTES = 64 * 1024
MAX_HONCHO_FACTS = 12
MAX_WIKI_CANDIDATES = 3
MAX_EVIDENCE_IDS = 16
_REDACTION_MARKER = "[REDACTED_CREDENTIAL]"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_EVIDENCE_ID_RE = re.compile(
    r"^(?:(?:issue|pr|commit|run|task|round)\s+"
    r"#?[A-Za-z0-9][A-Za-z0-9._:/@-]{0,119}|[0-9a-f]{7,64}|#[0-9]{1,20})$",
    re.IGNORECASE,
)
_SAFE_WIKI_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SAFE_WIKI_PATH_PATTERN = (
    r"^(?:pages/log\.md|pages/(?:team|nodes)/[A-Za-z0-9][A-Za-z0-9._-]*/"
    r"(?:[A-Za-z0-9][A-Za-z0-9._-]*/)*[A-Za-z0-9][A-Za-z0-9._-]*\.md)$"
)
_DIRECTIVE_RE = re.compile(
    r"(?:<\s*/?\s*(?:system|developer)\s*>|"
    r"\b(?:ignore|disregard|forget|override)\s+(?:all\s+)?"
    r"(?:previous|prior|above|system)\s+(?:instructions?|rules?|prompts?)\b|"
    r"\byou\s+are\s+now\s+(?:the\s+)?system\b|"
    r"\bsystem\s+prompt\s*[:=])",
    re.IGNORECASE,
)
_CREDENTIAL_PATTERNS = (
    re.compile(
        r"(?:\bauthorization\s*:\s*)?\bbearer\s+[A-Za-z0-9._~+/=-]{16,}",
        re.IGNORECASE,
    ),
    re.compile(r"\b[0-9]{6,12}:[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bgh(?:p|o|u|s|r)_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(
        r"\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|"
        r"secret|password)\s*[:=]\s*[^\s,;]{12,}",
        re.IGNORECASE,
    ),
    re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?"
        r"(?:-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\Z)",
        re.IGNORECASE | re.DOTALL,
    ),
)
EvidenceId = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=_EVIDENCE_ID_RE.pattern),
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _validate_timestamp(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return value


def _validate_text(
    value: str,
    *,
    field: str,
    max_chars: int,
    max_bytes: int,
    allow_empty: bool = False,
    reject_directive: bool = False,
) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    if not allow_empty and not value:
        raise ValueError(f"{field} must not be empty")
    if len(value) > max_chars or len(value.encode("utf-8")) > max_bytes:
        raise ValueError(f"{field} exceeds its character or UTF-8 byte limit")
    if _contains_credential(value):
        raise ValueError(f"{field} contains credential-like text")
    if reject_directive and _DIRECTIVE_RE.search(value):
        raise ValueError(f"{field} contains directive-like text")
    return value


def _contains_credential(value: str) -> bool:
    return any(pattern.search(value) for pattern in _CREDENTIAL_PATTERNS)


def _redact_credentials(value: str) -> str:
    redacted = value
    for pattern in _CREDENTIAL_PATTERNS:
        redacted = pattern.sub(_REDACTION_MARKER, redacted)
    return redacted


class ExtractionMessage(_StrictModel):
    role: Literal["user", "assistant"]
    text: str = Field(min_length=1, max_length=MAX_INPUT_MESSAGE_BYTES)

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        return _validate_text(
            value,
            field="messages.text",
            max_chars=MAX_INPUT_MESSAGE_BYTES,
            max_bytes=MAX_INPUT_MESSAGE_BYTES,
        )


class DistillExtractionInput(_StrictModel):
    schema_version: Literal[1]
    provider: Literal["codex"]
    content_trust: Literal["untrusted"]
    source_thread_hash: str = Field(pattern=_SHA256_RE.pattern)
    trigger: DistillTrigger
    captured_at: str
    truncated: StrictBool
    messages: tuple[ExtractionMessage, ...] = Field(max_length=MAX_INPUT_MESSAGES)
    message_count: StrictInt = Field(ge=0, le=MAX_INPUT_MESSAGES)
    byte_count: StrictInt = Field(ge=0, le=MAX_INPUT_BYTES)

    @field_validator("schema_version", mode="before")
    @classmethod
    def validate_schema_version(cls, value: object) -> object:
        if type(value) is not int or value != DISTILL_EXTRACTION_SCHEMA_VERSION:
            raise ValueError("schema_version must be integer 1")
        return value

    @field_validator("source_thread_hash")
    @classmethod
    def validate_source_thread_hash(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("source_thread_hash must be a SHA-256 hex digest")
        return value

    @field_validator("captured_at")
    @classmethod
    def validate_captured_at(cls, value: str) -> str:
        return _validate_timestamp(value, field="captured_at")

    def model_post_init(self, __context: Any) -> None:
        del __context
        if self.message_count != len(self.messages):
            raise ValueError("message_count does not match messages")
        actual_bytes = sum(len(message.text.encode("utf-8")) for message in self.messages)
        if self.byte_count != actual_bytes:
            raise ValueError("byte_count does not match messages")


class DistillProvenance(_StrictModel):
    provider: Literal["codex"]
    source_thread_hash: str = Field(pattern=_SHA256_RE.pattern)
    trigger: DistillTrigger
    distilled_at: str = Field(json_schema_extra={"format": "date-time"})

    @field_validator("source_thread_hash")
    @classmethod
    def validate_source_thread_hash(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("source_thread_hash must be a SHA-256 hex digest")
        return value

    @field_validator("distilled_at")
    @classmethod
    def validate_distilled_at(cls, value: str) -> str:
        return _validate_timestamp(value, field="distilled_at")


class HonchoFact(_StrictModel):
    kind: Literal["preference", "decision", "observation", "context"]
    text: str = Field(min_length=1, max_length=4096)
    subject: Literal["user", "session", "node"]

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        return _validate_text(
            value,
            field="honcho.text",
            max_chars=4096,
            max_bytes=4096,
            reject_directive=True,
        )


class WikiCandidate(_StrictModel):
    title: str = Field(min_length=1, max_length=160)
    suggested_path: str = Field(pattern=_SAFE_WIKI_PATH_PATTERN)
    summary: str = Field(min_length=1, max_length=600)
    evidence_excerpt: str = Field(min_length=1, max_length=200)

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        return _validate_text(
            value,
            field="wiki_candidates.title",
            max_chars=160,
            max_bytes=160,
            reject_directive=True,
        )

    @field_validator("suggested_path")
    @classmethod
    def validate_suggested_path(cls, value: str) -> str:
        if not isinstance(value, str) or not value or "\\" in value:
            raise ValueError("suggested_path must be a safe relative Wiki path")
        if value == "pages/log.md":
            return value
        path = PurePosixPath(value)
        parts = path.parts
        if (
            path.is_absolute()
            or len(parts) < 4
            or parts[0] != "pages"
            or parts[1] not in {"team", "nodes"}
            or path.suffix != ".md"
            or any(part in {".", ".."} for part in parts)
            or any(not _SAFE_WIKI_SEGMENT_RE.fullmatch(part) for part in parts[2:])
        ):
            raise ValueError("suggested_path is outside approved Wiki targets")
        return value

    @field_validator("summary")
    @classmethod
    def validate_summary(cls, value: str) -> str:
        return _validate_text(
            value,
            field="wiki_candidates.summary",
            max_chars=600,
            max_bytes=600,
            reject_directive=True,
        )

    @field_validator("evidence_excerpt")
    @classmethod
    def validate_evidence_excerpt(cls, value: str) -> str:
        return _validate_text(
            value,
            field="wiki_candidates.evidence_excerpt",
            max_chars=200,
            max_bytes=200,
            reject_directive=True,
        )


class ResumeState(_StrictModel):
    last_activity: str = Field(max_length=160)
    pending_action: str = Field(max_length=400)
    awaiting_user: StrictBool
    open_question: str = Field(max_length=400)
    next_step: str = Field(max_length=400)
    evidence: tuple[EvidenceId, ...] = Field(max_length=MAX_EVIDENCE_IDS)

    @field_validator("last_activity")
    @classmethod
    def validate_last_activity(cls, value: str) -> str:
        return _validate_text(
            value,
            field="resume.last_activity",
            max_chars=160,
            max_bytes=160,
            allow_empty=True,
        )

    @field_validator("pending_action", "open_question", "next_step")
    @classmethod
    def validate_optional_resume_text(cls, value: str, info: Any) -> str:
        return _validate_text(
            value,
            field=f"resume.{info.field_name}",
            max_chars=400,
            max_bytes=400,
            allow_empty=True,
        )

    @field_validator("evidence")
    @classmethod
    def validate_evidence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            if (
                not isinstance(item, str)
                or len(item) > 128
                or len(item.encode("utf-8")) > 128
                or not _EVIDENCE_ID_RE.fullmatch(item)
            ):
                raise ValueError("resume.evidence contains an invalid evidence identifier")
        return value


class DistillExtractionOutput(_StrictModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={
            "$id": "https://schemas.seoyoon-family.com/ccc-node/codex-distill-extraction-v1.json",
            "$schema": "https://json-schema.org/draft/2020-12/schema",
        },
    )

    schema_version: Literal[1]
    provenance: DistillProvenance
    honcho: tuple[HonchoFact, ...] = Field(max_length=MAX_HONCHO_FACTS)
    wiki_candidates: tuple[WikiCandidate, ...] = Field(max_length=MAX_WIKI_CANDIDATES)
    resume: ResumeState

    @field_validator("schema_version", mode="before")
    @classmethod
    def validate_schema_version(cls, value: object) -> object:
        if type(value) is not int or value != DISTILL_EXTRACTION_SCHEMA_VERSION:
            raise ValueError("schema_version must be integer 1")
        return value


@runtime_checkable
class DistillBackend(Protocol):
    """Provider-neutral extraction interface implemented by a later child."""

    async def extract(
        self, extraction_input: DistillExtractionInput
    ) -> DistillExtractionOutput: ...


def build_extraction_input(
    snapshot: CodexTranscriptSnapshot,
    *,
    trigger: DistillTrigger,
) -> DistillExtractionInput:
    """Normalize a bounded snapshot and redact credentials before provider use."""
    if not isinstance(snapshot, CodexTranscriptSnapshot):
        raise ValueError("snapshot must be a CodexTranscriptSnapshot")
    if not isinstance(trigger, DistillTrigger):
        raise ValueError("trigger must be a supported DistillTrigger")
    messages = tuple(
        ExtractionMessage(role=message.role, text=_redact_credentials(message.text))
        for message in snapshot.messages
    )
    return DistillExtractionInput(
        schema_version=DISTILL_EXTRACTION_SCHEMA_VERSION,
        provider="codex",
        content_trust="untrusted",
        source_thread_hash=snapshot.thread_hash,
        trigger=trigger,
        captured_at=snapshot.captured_at,
        truncated=snapshot.truncated,
        messages=messages,
        message_count=len(messages),
        byte_count=sum(len(message.text.encode("utf-8")) for message in messages),
    )


def canonical_extraction_input_bytes(value: DistillExtractionInput) -> bytes:
    """Serialize extraction input deterministically for a provider stdin boundary."""
    if not isinstance(value, DistillExtractionInput):
        raise ValueError("value must be a DistillExtractionInput")
    return json.dumps(
        value.model_dump(mode="json"),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def parse_extraction_output(
    payload: str | bytes,
    *,
    wiki_enabled: bool,
) -> DistillExtractionOutput:
    """Parse future provider output through strict syntax, schema, and privacy gates."""
    if type(wiki_enabled) is not bool:
        raise ValueError("wiki_enabled must be boolean")
    if isinstance(payload, str):
        encoded = payload.encode("utf-8")
        text = payload
    elif isinstance(payload, bytes):
        encoded = payload
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("extraction output must be valid UTF-8 JSON") from exc
    else:
        raise ValueError("extraction output must be text or bytes")
    if len(encoded) > MAX_EXTRACTION_JSON_BYTES:
        raise ValueError("extraction output is too large in bytes")
    try:
        raw = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
        )
    except json.JSONDecodeError as exc:
        raise ValueError("invalid extraction JSON") from exc
    if not isinstance(raw, dict):
        raise ValueError("extraction output JSON must be an object")
    result = DistillExtractionOutput.model_validate(raw)
    if not wiki_enabled and result.wiki_candidates:
        raise ValueError("wiki_candidates must be empty when Wiki output is disabled")
    return result


def extraction_output_json_schema() -> dict[str, Any]:
    """Return the strict provider output schema checked into ``schemas/``."""
    return DistillExtractionOutput.model_json_schema(mode="validation")


def build_extraction_diagnostics(
    extraction_input: DistillExtractionInput,
    *,
    output: DistillExtractionOutput | None,
    status: str,
) -> dict[str, str | int]:
    """Build body-free metrics suitable for logs and status surfaces."""
    if not isinstance(extraction_input, DistillExtractionInput):
        raise ValueError("extraction_input must be a DistillExtractionInput")
    if output is not None and not isinstance(output, DistillExtractionOutput):
        raise ValueError("output must be a DistillExtractionOutput or null")
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", status):
        raise ValueError("status must be a safe diagnostic code")
    return {
        "provider": extraction_input.provider,
        "source_thread_hash": extraction_input.source_thread_hash,
        "trigger": extraction_input.trigger.value,
        "message_count": extraction_input.message_count,
        "input_bytes": len(canonical_extraction_input_bytes(extraction_input)),
        "honcho_count": len(output.honcho) if output is not None else 0,
        "wiki_candidate_count": len(output.wiki_candidates) if output is not None else 0,
        "status": status,
    }
