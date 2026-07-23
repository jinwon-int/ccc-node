"""Codex-native skill-candidate collection (#667, follow-up to #643).

A parallel to the memory-distill extraction that turns a bounded, redacted
Codex transcript snapshot into **skill candidates** and stages them as
pending-draft directories that the provider-aware ``autoinstall.sh``
(``CCC_SKILL_PROVIDER=codex``) consumes. The candidate schema is deliberately
**separate** from ``DistillExtractionOutput`` (memory facts) — this module
reuses the neutral transport types (``DistillProvenance``, ``DistillTrigger``,
``CodexTranscriptSnapshot``) but never the memory-fact schema or sinks.

Nothing here connects to the live bot loops or the distill journal. The
collector accepts an already bounded/redacted snapshot and a backend that
returns a validated ``SkillCandidateOutput``; the sink writes owner-only,
idempotent pending-draft dirs. Wiring the collector into the bridge runtime
(trigger fan-out, poll loop, a real ``codex exec`` backend) is a separate
canary-gated change.
"""

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
from typing import Iterator, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from telegram_bot.utils.secure_fs import _atomic_write_bytes, ensure_private_directory

from .distill_extraction import DistillProvenance
from .distill_types import CodexTranscriptSnapshot

# Candidate bounds. Kept small: this is a review queue, not a bulk importer.
_MAX_CANDIDATES = 2
_MAX_SKILL_MD_BYTES = 16 * 1024
_MAX_OUTPUT_JSON_BYTES = 64 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_KEBAB_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9._-]+")

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
_CREDENTIAL_PATTERNS = (
    re.compile(r"(?:\bauthorization\s*:\s*)?\bbearer\s+[A-Za-z0-9._~+/=-]{16,}", re.IGNORECASE),
    re.compile(r"\bgh(?:p|o|u|s|r)_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(
        r"\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|secret|password)"
        r"\s*[:=]\s*[^\s,;]{12,}",
        re.IGNORECASE,
    ),
    re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
        re.IGNORECASE,
    ),
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SkillCandidate(_StrictModel):
    """One reusable-procedure proposal, ready to stage as a pending draft."""

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
        # Frontmatter is required so the draft can install (autoinstall lints it too).
        if not value.startswith("---"):
            raise ValueError("skill_md must start with YAML frontmatter")
        for text in (value,):
            if _DIRECTIVE_RE.search(text):
                raise ValueError("skill_md contains an injected directive")
            for pattern in _CREDENTIAL_PATTERNS:
                if pattern.search(text):
                    raise ValueError("skill_md contains a credential-like value")
        return value

    @field_validator("summary", "reason", "evidence_excerpt")
    @classmethod
    def _validate_free_text(cls, value: str) -> str:
        if _DIRECTIVE_RE.search(value):
            raise ValueError("field contains an injected directive")
        for pattern in _CREDENTIAL_PATTERNS:
            if pattern.search(value):
                raise ValueError("field contains a credential-like value")
        return value


class SkillCandidateOutput(_StrictModel):
    """Validated backend output. NOT a ``DistillExtractionOutput``."""

    schema_version: int = Field(ge=1, le=1)
    provenance: DistillProvenance
    candidates: tuple[SkillCandidate, ...] = Field(default=())

    @field_validator("candidates")
    @classmethod
    def _validate_candidates(cls, value: tuple[SkillCandidate, ...]) -> tuple[SkillCandidate, ...]:
        if len(value) > _MAX_CANDIDATES:
            raise ValueError(f"at most {_MAX_CANDIDATES} candidates")
        names = [candidate.name for candidate in value]
        if len(set(names)) != len(names):
            raise ValueError("candidate names must be unique")
        return value


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
    (``<safe_id>/{SKILL.md,meta.json}``), so the merged provider-aware installer
    (``CCC_SKILL_PROVIDER=codex``) installs them into ``CODEX_HOME/skills``.
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

    @staticmethod
    def _safe_id(job_id: str, index: int, name: str) -> str:
        raw = f"{job_id[:16]}-{index}-{name}"
        return _SAFE_ID_RE.sub("-", raw)[:160]

    def _job_record(self, output: SkillCandidateOutput, *, job_id: str, staged: list[str]) -> bytes:
        provenance = output.provenance
        record = {
            "schema_version": output.schema_version,
            "job_id": job_id,
            "review_status": "staged",
            "provenance": {
                "provider": provenance.provider,
                "source_thread_hash": provenance.source_thread_hash,
                "trigger": provenance.trigger.value,
                "distilled_at": provenance.distilled_at,
            },
            "staged_drafts": staged,
        }
        return json.dumps(
            record,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def _write_draft(self, candidate: SkillCandidate, *, safe_id: str, output: SkillCandidateOutput) -> None:
        provenance = output.provenance
        dest = self.pending_dir / safe_id
        # Deterministic dir name: a retried job maps to the same path. If the
        # draft dir already exists (or was archived by a prior install) the job
        # record guard above already made this a no-op, so we only reach here on
        # a genuinely new job.
        os.makedirs(dest, mode=0o700, exist_ok=True)
        _atomic_write_bytes(dest / "SKILL.md", candidate.skill_md.encode("utf-8"))
        meta = {
            "id": safe_id,
            "name": candidate.name,
            "status": "pending",
            "session_id": provenance.source_thread_hash,
            "trigger": provenance.trigger.value,
            "staged_at": provenance.distilled_at,
            "source": "codex-skill-collector",
            "summary": candidate.summary,
            "reason": candidate.reason,
        }
        _atomic_write_bytes(
            dest / "meta.json",
            json.dumps(meta, ensure_ascii=False, allow_nan=False, sort_keys=True).encode("utf-8"),
        )

    def write(self, output: SkillCandidateOutput, *, job_id: str) -> SkillCandidateStageResult:
        if not isinstance(output, SkillCandidateOutput):
            raise ValueError("output must be a validated SkillCandidateOutput")
        if not isinstance(job_id, str) or not _SHA256_RE.fullmatch(job_id):
            raise ValueError("job_id must be a SHA-256 hex digest")
        count = len(output.candidates)
        with self._exclusive():
            record_path = self.queue_dir / f"{job_id}.json"
            self._validate_regular_file(record_path)
            if count == 0:
                return SkillCandidateStageResult(0, False)
            staged = [
                self._safe_id(job_id, index, candidate.name)
                for index, candidate in enumerate(output.candidates)
            ]
            payload = self._job_record(output, job_id=job_id, staged=staged)
            if record_path.exists():
                if record_path.read_bytes() == payload:
                    return SkillCandidateStageResult(count, False)
                raise SkillCandidateCollisionError("skill-candidate job collision")
            os.makedirs(self.pending_dir, mode=0o700, exist_ok=True)
            for candidate, safe_id in zip(output.candidates, staged):
                self._write_draft(candidate, safe_id=safe_id, output=output)
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
        if output.provenance != provenance:
            raise ValueError("backend altered the provenance")
        return self._sink.write(output, job_id=job_id)


__all__ = [
    "SkillCandidate",
    "SkillCandidateOutput",
    "SkillCandidateParseError",
    "parse_skill_candidate_output",
    "SkillCandidateBackend",
    "SkillCandidateSink",
    "SkillCandidateCollector",
    "SkillCandidateStageResult",
    "SkillCandidateCollisionError",
]
