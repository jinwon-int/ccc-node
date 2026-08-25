"""Codex-native skill-candidate collection (#667, follow-up to #643).

A parallel to the memory-distill extraction that turns a bounded, redacted
Codex transcript snapshot into **skill candidates** and stages them as
pending-draft directories that the provider-aware ``autoinstall.sh``
(``CCC_SKILL_PROVIDER=codex``) consumes. The candidate schema is deliberately
**separate** from ``DistillExtractionOutput`` (memory facts) — this module
reuses the neutral transport types (``DistillProvenance``, ``DistillTrigger``,
``CodexTranscriptSnapshot``) but never the memory-fact schema or sinks.

Nothing in this module connects to the live bot loop or mutates the distill
journal. The runtime worker supplies an already bounded/redacted snapshot and a
backend that returns a validated ``SkillCandidateOutput``; the sink writes
owner-only, idempotent pending-draft dirs.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
from pathlib import PurePosixPath
import re
import stat
import threading
from typing import Annotated, Iterator, Literal, Protocol, cast, runtime_checkable
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from telegram_bot.utils.redaction import CREDENTIAL_PATTERNS as _CREDENTIAL_PATTERNS
from telegram_bot.utils.secure_fs import _atomic_write_bytes, ensure_private_directory

from .distill_extraction import DistillProvenance
from .distill_types import CodexTranscriptSnapshot

# Candidate bounds. Kept small: this is a review queue, not a bulk importer.
_MAX_CANDIDATES = 2
_MAX_SKILL_MD_BYTES = 16 * 1024
_MAX_PATCH_TEXT_BYTES = 32 * 1024
_MAX_SUPPORT_FILE_BYTES = 48 * 1024
_MAX_OUTPUT_JSON_BYTES = 64 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_KEBAB_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SAFE_PATH_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9._-]+")
_SAFE_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_RETRY_SCHEMA_VERSION = 1

# Redaction / injection guards applied to the skill body before it is ever
# staged, so a leaked credential or a prompt-injection directive fails closed
# (the whole candidate is rejected) instead of landing in a pending draft.
# Mirrors the distill extraction scanner family; reason labels never quote the
# offending bytes.
_DIRECTIVE_RE = re.compile(
    r"(?:<\s*/?\s*(?:system|developer)\s*>|"
    r"\b(?:ignore|disregard|forget|override)\s+(?:all\s+)?"
    r"(?:previous|prior|above|system)\s+(?:instructions?|rules?|prompts?)\b|"
    r"\byou\s+are\s+now\s+(?:the\s+)?system\b|"
    r"\bsystem\s+prompt\s*[:=])",
    re.IGNORECASE,
)
# Credential patterns are the shared canonical set (bridge/utils/redaction.py);
# imported above as _CREDENTIAL_PATTERNS so the validators/backend are unchanged.


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# The legacy schema-v1 SkillCandidate model used to live here "as an input
# adapter", but v1 payloads are upgraded as raw dicts by
# SkillCandidateOutput._upgrade_schema_v1 and validated by the proposal
# classes below — the model was never instantiated, and its duplicated
# validators could silently drift from SkillCreateProposal's.


def _validate_public_text(value: str) -> str:
    if _DIRECTIVE_RE.search(value):
        raise ValueError("field contains an injected directive")
    for pattern in _CREDENTIAL_PATTERNS:
        if pattern.search(value):
            raise ValueError("field contains a credential-like value")
    return value


def _validate_relative_target(value: str, *, create_only: bool) -> str:
    if (
        not value
        or "\x00" in value
        or "\\" in value
        or value.startswith("/")
        or "//" in value
    ):
        raise ValueError("relative_target must be a canonical POSIX relative path")
    parts = value.split("/")
    if any(
        part in {"", ".", ".."} or not _SAFE_PATH_COMPONENT_RE.fullmatch(part)
        for part in parts
    ):
        raise ValueError("relative_target contains an unsafe path component")
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or candidate.as_posix() != value:
        raise ValueError("relative_target must be canonical")
    if create_only:
        if parts[0] not in {"references", "scripts", "templates"} or len(parts) < 2:
            raise ValueError("write_file target must be under an allowlisted support directory")
    elif value != "SKILL.md" and (
        parts[0] not in {"references", "scripts", "templates"} or len(parts) < 2
    ):
        raise ValueError("patch target must be SKILL.md or an allowlisted support file")
    return value


class SkillCreateProposal(_StrictModel):
    action: Literal["create"]
    name: str = Field(min_length=1, max_length=64)
    summary: str = Field(min_length=1, max_length=600)
    reason: str = Field(min_length=1, max_length=600)
    evidence_excerpt: str = Field(default="", max_length=200)
    skill_md: str = Field(min_length=1, max_length=_MAX_SKILL_MD_BYTES)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if not _KEBAB_RE.fullmatch(value):
            raise ValueError("name must be lowercase kebab-case")
        return value

    @field_validator("skill_md")
    @classmethod
    def _validate_skill_md(cls, value: str) -> str:
        if not value.startswith("---"):
            raise ValueError("skill_md must start with YAML frontmatter")
        if len(value.encode("utf-8")) > _MAX_SKILL_MD_BYTES:
            raise ValueError("skill_md exceeds UTF-8 byte bound")
        return _validate_public_text(value)

    @field_validator("summary", "reason", "evidence_excerpt")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        return _validate_public_text(value)


class SkillPatchProposal(_StrictModel):
    action: Literal["patch"]
    target_skill: str = Field(min_length=1, max_length=64)
    relative_target: str = Field(min_length=1, max_length=240)
    expected_sha256: str
    old_text: str = Field(min_length=1, max_length=_MAX_PATCH_TEXT_BYTES)
    new_text: str = Field(max_length=_MAX_PATCH_TEXT_BYTES)
    improvement_reason: str = Field(min_length=1, max_length=600)
    reason: str = Field(min_length=1, max_length=600)
    evidence_excerpt: str = Field(default="", max_length=200)

    @field_validator("target_skill")
    @classmethod
    def _validate_target_skill(cls, value: str) -> str:
        if not _KEBAB_RE.fullmatch(value):
            raise ValueError("target_skill must be lowercase kebab-case")
        return value

    @field_validator("relative_target")
    @classmethod
    def _validate_target(cls, value: str) -> str:
        return _validate_relative_target(value, create_only=False)

    @field_validator("expected_sha256")
    @classmethod
    def _validate_sha(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("expected_sha256 must be lowercase SHA-256")
        return value

    @field_validator("old_text", "new_text")
    @classmethod
    def _validate_patch_text(cls, value: str) -> str:
        if len(value.encode("utf-8")) > _MAX_PATCH_TEXT_BYTES:
            raise ValueError("patch text exceeds UTF-8 byte bound")
        return _validate_public_text(value)

    @field_validator("improvement_reason", "reason", "evidence_excerpt")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        return _validate_public_text(value)


class SkillWriteFileProposal(_StrictModel):
    action: Literal["write_file"]
    target_skill: str = Field(min_length=1, max_length=64)
    relative_target: str = Field(min_length=1, max_length=240)
    expected_absent: Literal[True]
    expected_provenance_revision: int = Field(ge=0)
    expected_provenance_sha256: str
    content: str = Field(min_length=1, max_length=_MAX_SUPPORT_FILE_BYTES)
    improvement_reason: str = Field(min_length=1, max_length=600)
    reason: str = Field(min_length=1, max_length=600)
    evidence_excerpt: str = Field(default="", max_length=200)

    @field_validator("target_skill")
    @classmethod
    def _validate_target_skill(cls, value: str) -> str:
        if not _KEBAB_RE.fullmatch(value):
            raise ValueError("target_skill must be lowercase kebab-case")
        return value

    @field_validator("relative_target")
    @classmethod
    def _validate_target(cls, value: str) -> str:
        return _validate_relative_target(value, create_only=True)

    @field_validator("expected_provenance_sha256")
    @classmethod
    def _validate_sha(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("expected_provenance_sha256 must be lowercase SHA-256")
        return value

    @field_validator("content")
    @classmethod
    def _validate_content(cls, value: str) -> str:
        if len(value.encode("utf-8")) > _MAX_SUPPORT_FILE_BYTES:
            raise ValueError("content exceeds UTF-8 byte bound")
        return _validate_public_text(value)

    @field_validator("improvement_reason", "reason", "evidence_excerpt")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        return _validate_public_text(value)


class SkillNoopProposal(_StrictModel):
    action: Literal["noop"]
    reason: str = Field(min_length=1, max_length=600)
    evidence_excerpt: str = Field(default="", max_length=200)

    @field_validator("reason", "evidence_excerpt")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        return _validate_public_text(value)


SkillProposal = Annotated[
    SkillCreateProposal | SkillPatchProposal | SkillWriteFileProposal | SkillNoopProposal,
    Field(discriminator="action"),
]


class SkillCandidateOutput(_StrictModel):
    """Validated schema-v2 backend output with a schema-v1 input adapter."""

    schema_version: Literal[2]
    provenance: DistillProvenance
    proposals: tuple[SkillProposal, ...] = Field(default=())

    @model_validator(mode="before")
    @classmethod
    def _upgrade_schema_v1(cls, value: object) -> object:
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            return value
        legacy = dict(value)
        raw_candidates = legacy.pop("candidates", None)
        if not isinstance(raw_candidates, (list, tuple)):
            return value
        proposals: list[dict[str, object]] = []
        for raw in raw_candidates:
            if not isinstance(raw, dict):
                return value
            proposals.append({"action": "create", **raw})
        legacy["schema_version"] = 2
        legacy["proposals"] = proposals
        return legacy

    @field_validator("proposals")
    @classmethod
    def _validate_proposals(
        cls, value: tuple[SkillProposal, ...]
    ) -> tuple[SkillProposal, ...]:
        if len(value) > _MAX_CANDIDATES:
            raise ValueError(f"at most {_MAX_CANDIDATES} proposals")
        identities = [
            (
                proposal.action,
                getattr(proposal, "name", None)
                or getattr(proposal, "target_skill", None),
                getattr(proposal, "relative_target", None),
            )
            for proposal in value
        ]
        if len(set(identities)) != len(identities):
            raise ValueError("proposal identities must be unique")
        return value

    @property
    def candidates(self) -> tuple[SkillCreateProposal, ...]:
        """Compatibility view for callers that only consumed v1 creates."""

        return tuple(
            proposal
            for proposal in self.proposals
            if isinstance(proposal, SkillCreateProposal)
        )


class SkillCandidateParseError(ValueError):
    """The backend payload is not a valid ``SkillCandidateOutput``."""


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    seen: dict[str, object] = {}
    for key, val in pairs:
        if key in seen:
            raise SkillCandidateParseError("duplicate key in skill-candidate output")
        seen[key] = val
    return seen


def _reject_constant(value: str) -> object:
    raise SkillCandidateParseError("non-finite number in skill-candidate output")


def parse_skill_candidate_output(payload: str | bytes) -> SkillCandidateOutput:
    """Strictly parse and validate a backend payload, fail-closed."""

    raw = payload.encode("utf-8") if isinstance(payload, str) else payload
    if len(raw) > _MAX_OUTPUT_JSON_BYTES:
        raise SkillCandidateParseError("skill-candidate output exceeds size bound")
    try:
        data = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SkillCandidateParseError("skill-candidate output is not valid JSON") from exc
    try:
        return SkillCandidateOutput.model_validate(data)
    except ValueError as exc:
        raise SkillCandidateParseError("skill-candidate output failed validation") from exc


@runtime_checkable
class SkillCandidateBackend(Protocol):
    """A backend that drafts skill candidates from a bounded snapshot."""

    async def extract(
        self,
        *,
        snapshot: CodexTranscriptSnapshot,
        provenance: DistillProvenance,
    ) -> SkillCandidateOutput: ...


@dataclass(frozen=True, slots=True)
class SkillCandidateStageResult:
    candidates_staged: int
    record_written: bool


class SkillCandidateCollisionError(ValueError):
    """An existing job record differs from its immutable candidate payload."""


class SkillCandidateSink:
    """Idempotent, owner-only writer of pending-draft directories.

    One immutable JSON record per job id (in ``queue_dir``) makes re-processing
    the same checkpoint a no-op — so the same snapshot handled many times at
    once stages each draft exactly once. The drafts themselves are written into
    ``pending_dir`` in the exact contract ``autoinstall.sh`` consumes
    (``<safe_id>/{proposal.json,meta.json}``), so the provider-aware installer
    can route create and incremental actions without mixed payloads.
    """

    def __init__(self, queue_dir: Path, pending_dir: Path) -> None:
        self.queue_dir = Path(os.path.abspath(os.fspath(queue_dir)))
        self.pending_dir = Path(os.path.abspath(os.fspath(pending_dir)))
        self._lock_path = self.queue_dir / ".skill-candidate-sink.lock"
        self._thread_lock = threading.RLock()

    @staticmethod
    def _validate_regular_file(path: Path) -> None:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise PermissionError(f"skill-candidate state must be regular: {path}")
        if metadata.st_nlink != 1:
            raise PermissionError("skill-candidate state must not have hard links")
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise PermissionError("skill-candidate state is not owned by this process")

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
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                yield
            finally:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)

    def has(self, job_id: str) -> bool:
        """True when a job's candidates were already staged (marker present).

        Lets a collector skip the expensive backend call for jobs it already
        processed — the write path stays idempotent regardless.
        """
        if not isinstance(job_id, str) or not _SHA256_RE.fullmatch(job_id):
            return False
        return (self.queue_dir / f"{job_id}.json").exists()

    def _retry_path(self, job_id: str) -> Path:
        if not isinstance(job_id, str) or not _SHA256_RE.fullmatch(job_id):
            raise ValueError("job_id must be a SHA-256 hex digest")
        return self.queue_dir / ".retries" / f"{job_id}.json"

    def _claim_path(self, job_id: str) -> Path:
        if not isinstance(job_id, str) or not _SHA256_RE.fullmatch(job_id):
            raise ValueError("job_id must be a SHA-256 hex digest")
        return self.queue_dir / ".claims" / f"{job_id}.lock"

    @contextmanager
    def claim(self, job_id: str) -> Iterator[bool]:
        """Try to own one provider attempt for ``job_id`` across processes.

        The empty owner-only lock file intentionally persists so every process
        always locks the same inode. A contending collector returns ``False``
        immediately instead of waiting and later replaying the provider call.
        """

        path = self._claim_path(job_id)
        ensure_private_directory(self.queue_dir)
        ensure_private_directory(path.parent)
        self._validate_regular_file(path)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        acquired = False
        try:
            os.fchmod(descriptor, 0o600)
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or (
                    hasattr(os, "getuid")
                    and metadata.st_uid != os.getuid()
                )
            ):
                raise PermissionError("skill-candidate claim is unsafe")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                yield False
                return
            yield True
        finally:
            try:
                if acquired:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def _read_retry_unlocked(self, path: Path, *, job_id: str) -> dict[str, object] | None:
        self._validate_regular_file(path)
        if not path.exists():
            return None
        try:
            record = json.loads(path.read_bytes())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("skill-candidate retry state is invalid") from exc
        if (
            not isinstance(record, dict)
            or record.get("schema_version") != _RETRY_SCHEMA_VERSION
            or record.get("job_id") != job_id
            or type(record.get("attempts")) is not int
            or int(record["attempts"]) < 1
            or not isinstance(record.get("next_retry_at"), (int, float))
            or isinstance(record.get("next_retry_at"), bool)
            or not math.isfinite(float(record["next_retry_at"]))
            or float(record["next_retry_at"]) < 0
            or not isinstance(record.get("error_code"), str)
            or _SAFE_ERROR_CODE_RE.fullmatch(str(record["error_code"])) is None
        ):
            raise ValueError("skill-candidate retry state is invalid")
        return record

    def retry_ready(self, job_id: str, *, now: float) -> bool:
        """Return whether a failed job's durable backoff window has elapsed.

        Corrupt/untrusted retry state raises and therefore fails closed before
        another provider call.
        """

        if (
            not isinstance(now, (int, float))
            or isinstance(now, bool)
            or not math.isfinite(float(now))
            or float(now) < 0
        ):
            raise ValueError("now must be a finite non-negative timestamp")
        path = self._retry_path(job_id)
        with self._exclusive():
            ensure_private_directory(path.parent)
            record = self._read_retry_unlocked(path, job_id=job_id)
            return record is None or float(
                cast(int | float, record["next_retry_at"])
            ) <= float(now)

    def record_retry_failure(
        self,
        job_id: str,
        *,
        error_code: str,
        now: float,
        base_delay_seconds: float,
        max_delay_seconds: float,
    ) -> None:
        """Durably apply exponential backoff using body-free error metadata."""

        if (
            not isinstance(error_code, str)
            or _SAFE_ERROR_CODE_RE.fullmatch(error_code) is None
            or not isinstance(now, (int, float))
            or isinstance(now, bool)
            or not math.isfinite(float(now))
            or float(now) < 0
            or not isinstance(base_delay_seconds, (int, float))
            or isinstance(base_delay_seconds, bool)
            or not math.isfinite(float(base_delay_seconds))
            or float(base_delay_seconds) <= 0
            or not isinstance(max_delay_seconds, (int, float))
            or isinstance(max_delay_seconds, bool)
            or not math.isfinite(float(max_delay_seconds))
            or float(max_delay_seconds) < float(base_delay_seconds)
        ):
            raise ValueError("invalid skill-candidate retry configuration")
        path = self._retry_path(job_id)
        with self._exclusive():
            ensure_private_directory(path.parent)
            current = self._read_retry_unlocked(path, job_id=job_id)
            attempts = (
                cast(int, current["attempts"]) + 1 if current is not None else 1
            )
            exponent = min(attempts - 1, 30)
            delay = min(
                float(max_delay_seconds),
                float(base_delay_seconds) * (2**exponent),
            )
            payload = json.dumps(
                {
                    "schema_version": _RETRY_SCHEMA_VERSION,
                    "job_id": job_id,
                    "attempts": attempts,
                    "error_code": error_code,
                    "next_retry_at": float(now) + delay,
                },
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            _atomic_write_bytes(path, payload)

    def clear_retry(self, job_id: str) -> None:
        """Remove retry metadata after a successful/zero-candidate stage."""

        path = self._retry_path(job_id)
        with self._exclusive():
            ensure_private_directory(path.parent)
            self._validate_regular_file(path)
            path.unlink(missing_ok=True)

    @staticmethod
    def _safe_id(job_id: str, index: int, name: str) -> str:
        raw = f"{job_id[:16]}-{index}-{name}"
        return _SAFE_ID_RE.sub("-", raw)[:160]

    def _job_record(
        self,
        output: SkillCandidateOutput,
        *,
        job_id: str,
        staged: list[str],
        noop_count: int,
    ) -> bytes:
        provenance = output.provenance
        record = {
            "schema_version": output.schema_version,
            "job_id": job_id,
            "review_status": "staged" if staged else "complete",
            "provenance": {
                "provider": provenance.provider,
                "source_thread_hash": provenance.source_thread_hash,
                "trigger": provenance.trigger.value,
                "distilled_at": provenance.distilled_at,
            },
            "staged_proposals": staged,
            "noop_count": noop_count,
        }
        return json.dumps(
            record,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @staticmethod
    def _proposal_id(
        proposal: SkillProposal,
        *,
        job_id: str,
        index: int,
        output: SkillCandidateOutput,
    ) -> str:
        canonical = json.dumps(
            {
                "schema_version": output.schema_version,
                "job_id": job_id,
                "index": index,
                "provenance": output.provenance.model_dump(mode="json"),
                "proposal": proposal.model_dump(mode="json"),
            },
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def _write_proposal(
        self,
        proposal: SkillProposal,
        *,
        safe_id: str,
        proposal_id: str,
        output: SkillCandidateOutput,
    ) -> None:
        provenance = output.provenance
        dest = self.pending_dir / safe_id
        payload = {
            "schema_version": 2,
            "proposal_id": proposal_id,
            "provenance": provenance.model_dump(mode="json"),
            "proposal": proposal.model_dump(mode="json"),
        }
        proposal_bytes = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        target_name = getattr(proposal, "name", None) or getattr(
            proposal, "target_skill", None
        )
        meta = {
            "id": safe_id,
            "proposal_id": proposal_id,
            "action": proposal.action,
            "name": target_name,
            "status": "pending",
            "session_id": provenance.source_thread_hash,
            "trigger": provenance.trigger.value,
            "staged_at": provenance.distilled_at,
            "source": "codex-skill-collector",
            "summary": getattr(proposal, "summary", ""),
            "reason": proposal.reason,
        }
        meta_bytes = json.dumps(
            meta,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
        ).encode("utf-8")
        ensure_private_directory(self.pending_dir)
        try:
            dest.lstat()
        except FileNotFoundError:
            pass
        else:
            if self._existing_proposal_matches(
                dest,
                proposal_bytes=proposal_bytes,
                meta_bytes=meta_bytes,
            ):
                return
            raise SkillCandidateCollisionError(
                "skill-candidate proposal collision"
            )
        temporary = self.pending_dir / f".stage-{safe_id}-{uuid.uuid4().hex}"
        os.mkdir(temporary, mode=0o700)
        try:
            _atomic_write_bytes(temporary / "proposal.json", proposal_bytes)
            _atomic_write_bytes(temporary / "meta.json", meta_bytes)
            directory_fd = os.open(
                temporary,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            os.rename(temporary, dest)
            parent_fd = os.open(
                self.pending_dir,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        except Exception:
            for name in ("proposal.json", "meta.json"):
                (temporary / name).unlink(missing_ok=True)
            try:
                temporary.rmdir()
            except OSError:
                pass
            raise

    @staticmethod
    def _read_exact_private(path: Path, *, max_bytes: int) -> bytes:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size > max_bytes
        ):
            raise SkillCandidateCollisionError("unsafe staged proposal")
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            opened = os.fstat(descriptor)
            if (
                (opened.st_dev, opened.st_ino)
                != (metadata.st_dev, metadata.st_ino)
                or not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != os.getuid()
                or opened.st_nlink != 1
                or stat.S_IMODE(opened.st_mode) != 0o600
                or opened.st_size > max_bytes
            ):
                raise SkillCandidateCollisionError("staged proposal changed")
            chunks: list[bytes] = []
            remaining = max_bytes + 1
            while remaining > 0:
                chunk = os.read(descriptor, min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            content = b"".join(chunks)
            after = os.fstat(descriptor)
            if (
                len(content) > max_bytes
                or (
                    opened.st_dev,
                    opened.st_ino,
                    opened.st_size,
                    opened.st_mtime_ns,
                    opened.st_ctime_ns,
                )
                != (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                )
            ):
                raise SkillCandidateCollisionError("staged proposal changed")
            return content
        finally:
            os.close(descriptor)

    @classmethod
    def _existing_proposal_matches(
        cls,
        dest: Path,
        *,
        proposal_bytes: bytes,
        meta_bytes: bytes,
    ) -> bool:
        metadata = dest.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise SkillCandidateCollisionError("unsafe staged proposal directory")
        entries = {entry.name for entry in os.scandir(dest)}
        if entries != {"proposal.json", "meta.json"}:
            return False
        return (
            cls._read_exact_private(
                dest / "proposal.json",
                max_bytes=_MAX_OUTPUT_JSON_BYTES,
            )
            == proposal_bytes
            and cls._read_exact_private(
                dest / "meta.json",
                max_bytes=16 * 1024,
            )
            == meta_bytes
        )

    def write(self, output: SkillCandidateOutput, *, job_id: str) -> SkillCandidateStageResult:
        if not isinstance(output, SkillCandidateOutput):
            raise ValueError("output must be a validated SkillCandidateOutput")
        if not isinstance(job_id, str) or not _SHA256_RE.fullmatch(job_id):
            raise ValueError("job_id must be a SHA-256 hex digest")
        staged_proposals = [
            proposal for proposal in output.proposals if proposal.action != "noop"
        ]
        count = len(staged_proposals)
        noop_count = len(output.proposals) - count
        with self._exclusive():
            record_path = self.queue_dir / f"{job_id}.json"
            self._validate_regular_file(record_path)
            staged: list[str] = []
            prepared: list[tuple[SkillProposal, str, str]] = []
            for index, proposal in enumerate(output.proposals):
                if proposal.action == "noop":
                    continue
                proposal_id = self._proposal_id(
                    proposal,
                    job_id=job_id,
                    index=index,
                    output=output,
                )
                target_name = getattr(proposal, "name", None) or getattr(
                    proposal, "target_skill", "proposal"
                )
                safe_id = self._safe_id(job_id, index, str(target_name))
                staged.append(safe_id)
                prepared.append((proposal, safe_id, proposal_id))
            payload = self._job_record(
                output,
                job_id=job_id,
                staged=staged,
                noop_count=noop_count,
            )
            if record_path.exists():
                if record_path.read_bytes() == payload:
                    return SkillCandidateStageResult(count, False)
                raise SkillCandidateCollisionError("skill-candidate job collision")
            # Stage drafts only when there are candidates; the job marker is
            # written for EVERY processed job (including zero-candidate ones) so
            # a barren snapshot is not re-drafted (and re-charged) on every
            # sweep — has() then dedupes it.
            if count > 0:
                ensure_private_directory(self.pending_dir)
                for proposal, safe_id, proposal_id in prepared:
                    self._write_proposal(
                        proposal,
                        safe_id=safe_id,
                        proposal_id=proposal_id,
                        output=output,
                    )
            # Marker written last: a crash mid-stage leaves gated drafts (safe),
            # never a "done" marker with no drafts.
            _atomic_write_bytes(record_path, payload)
            return SkillCandidateStageResult(count, True)


class SkillCandidateCollector:
    """Drive one snapshot through the backend into the pending-draft sink."""

    def __init__(self, backend: SkillCandidateBackend, sink: SkillCandidateSink) -> None:
        self._backend = backend
        self._sink = sink

    async def collect(
        self,
        *,
        snapshot: CodexTranscriptSnapshot,
        provenance: DistillProvenance,
        job_id: str,
    ) -> SkillCandidateStageResult:
        if provenance.source_thread_hash != snapshot.thread_hash:
            raise ValueError("provenance thread hash must match the snapshot")
        output = await self._backend.extract(snapshot=snapshot, provenance=provenance)
        # Identity fields only — the model generates its own distilled_at, so a
        # full-equality check would always fail (the backend enforces the same).
        echoed = output.provenance
        if (
            echoed.provider != provenance.provider
            or echoed.source_thread_hash != provenance.source_thread_hash
            or echoed.trigger != provenance.trigger
        ):
            raise ValueError("backend altered the provenance identity")
        return self._sink.write(output, job_id=job_id)


__all__ = [
    "SkillCreateProposal",
    "SkillPatchProposal",
    "SkillWriteFileProposal",
    "SkillNoopProposal",
    "SkillProposal",
    "SkillCandidateOutput",
    "SkillCandidateParseError",
    "parse_skill_candidate_output",
    "SkillCandidateBackend",
    "SkillCandidateSink",
    "SkillCandidateCollector",
    "SkillCandidateStageResult",
    "SkillCandidateCollisionError",
]
