"""Replay-safe owner-only local write-back for validated Codex distill output."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading
from typing import Iterator

from telegram_bot.utils.secure_fs import (
    _atomic_write_bytes,
    ensure_private_directory,
)

from .distill_extraction import DistillExtractionOutput


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_WORD_RE = re.compile(r"[0-9a-z가-힣]+")
_SUPPORTED_AUDIENCES = frozenset({"private", "shared"})
_MAX_EXISTING_FACT_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class LocalSinkWriteResult:
    facts_added: int
    resume_written: bool


def _normalize_fact(text: str) -> str:
    return " ".join(_WORD_RE.findall(text.lower()))


def _single_line(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _bounded_utf8(text: str, max_bytes: int) -> bytes:
    payload = text.encode("utf-8")
    if len(payload) <= max_bytes:
        return payload
    suffix = "\n… [truncated by CCC resume budget]\n".encode()
    budget = max(0, max_bytes - len(suffix))
    prefix = payload[:budget].decode("utf-8", errors="ignore").encode("utf-8")
    return prefix + suffix


class CodexLocalMemorySink:
    """Write one validated extraction into local facts and ``resume.md``.

    The state directory is an already-routed audience boundary. Only the
    non-identifying ``private``/``shared`` label reaches persisted facts; raw
    Telegram ids and raw Codex thread ids are not accepted by this API.
    """

    def __init__(
        self,
        state_dir: Path,
        *,
        audience: str,
        max_facts: int = 1000,
        max_resume_bytes: int = 4000,
    ) -> None:
        if audience not in _SUPPORTED_AUDIENCES:
            raise ValueError("local memory sink audience must be private or shared")
        if type(max_facts) is not int or max_facts <= 0:
            raise ValueError("max_facts must be a positive integer")
        if type(max_resume_bytes) is not int or max_resume_bytes < 256:
            raise ValueError("max_resume_bytes must be at least 256")
        self.state_dir = Path(os.path.abspath(os.fspath(state_dir)))
        self.audience = audience
        self.max_facts = max_facts
        self.max_resume_bytes = max_resume_bytes
        self._facts_path = self.state_dir / "memory-facts.jsonl"
        self._resume_path = self.state_dir / "resume.md"
        self._lock_path = self.state_dir / ".local-memory-sink.lock"
        self._thread_lock = threading.RLock()

    @staticmethod
    def _validate_regular_file(path: Path) -> None:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise PermissionError(f"local memory state must be a regular file: {path}")
        if metadata.st_nlink != 1:
            raise PermissionError("local memory state must not have hard links")
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise PermissionError("local memory state is not owned by this process")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise PermissionError("local memory state must have mode 0600")

    @classmethod
    def _validate_open_file(cls, descriptor: int, path: Path) -> None:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise PermissionError(f"local memory lock must be regular: {path}")
        if metadata.st_nlink != 1:
            raise PermissionError("local memory lock must not have hard links")
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise PermissionError("local memory lock is not owned by this process")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise PermissionError("local memory lock must have mode 0600")

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        ensure_private_directory(self.state_dir)
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

    def _read_existing_facts(self) -> tuple[list[str], set[str]]:
        self._validate_regular_file(self._facts_path)
        if not self._facts_path.exists():
            return [], set()
        payload = self._facts_path.read_bytes()
        if len(payload) > _MAX_EXISTING_FACT_BYTES:
            raise ValueError("local memory facts exceed the safe read bound")
        lines = [line for line in payload.decode("utf-8").splitlines() if line.strip()]
        seen: set[str] = set()
        for line in lines:
            try:
                value = json.loads(line)
            except (TypeError, ValueError):
                continue
            if isinstance(value, dict) and isinstance(value.get("text"), str):
                normalized = _normalize_fact(value["text"])
                if normalized:
                    seen.add(normalized)
        return lines, seen

    def _write_facts(
        self,
        output: DistillExtractionOutput,
        *,
        job_id: str,
    ) -> int:
        lines, seen = self._read_existing_facts()
        provenance = output.provenance
        added: list[str] = []
        for item in output.honcho:
            normalized = _normalize_fact(item.text)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            fact_id = (
                "distill-" + hashlib.sha256(f"{job_id}\0{normalized}".encode()).hexdigest()[:12]
            )
            fact = {
                "schema_version": output.schema_version,
                "id": fact_id,
                "kind": item.kind,
                "text": item.text,
                "review": "auto-local",
                "privacy": self.audience,
                "audience": self.audience,
                "durability": "durable",
                "confidence": 0.7,
                "observed_at": provenance.distilled_at,
                "entities": [item.subject],
                "tags": ["distilled", provenance.trigger.value],
                "source": {
                    "type": "distill",
                    "provider": provenance.provider,
                    "job_id": job_id,
                    "thread_hash": provenance.source_thread_hash,
                    "trigger": provenance.trigger.value,
                    "schema_version": output.schema_version,
                },
            }
            added.append(
                json.dumps(
                    fact,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        if not added:
            return 0
        bounded_lines = (lines + added)[-self.max_facts :]
        payload = ("\n".join(bounded_lines) + "\n").encode("utf-8")
        self._validate_regular_file(self._facts_path)
        _atomic_write_bytes(self._facts_path, payload)
        self._validate_regular_file(self._facts_path)
        return len(added)

    def _render_resume(self, output: DistillExtractionOutput) -> bytes | None:
        resume = output.resume
        rows = (
            ("마지막 작업", resume.last_activity),
            ("다음 액션", resume.pending_action),
            ("사용자 대기", "yes" if resume.awaiting_user else ""),
            ("열린 질문", resume.open_question),
            ("다음 한 수", resume.next_step),
            ("근거", ", ".join(resume.evidence)),
        )
        lines = [
            f"- {label}: {_single_line(value)}" for label, value in rows if _single_line(value)
        ]
        if not lines:
            return None
        provenance = output.provenance
        header = (
            "<!-- ccc-node:distill "
            f"schema={output.schema_version} provider={provenance.provider} "
            f"thread_hash={provenance.source_thread_hash} "
            f"trigger={provenance.trigger.value} "
            f"distilled_at={provenance.distilled_at} -->\n"
        )
        return _bounded_utf8(header + "\n".join(lines) + "\n", self.max_resume_bytes)

    def write(
        self,
        output: DistillExtractionOutput,
        *,
        job_id: str,
    ) -> LocalSinkWriteResult:
        if not isinstance(output, DistillExtractionOutput):
            raise ValueError("output must be a validated DistillExtractionOutput")
        if not isinstance(job_id, str) or not _SHA256_RE.fullmatch(job_id):
            raise ValueError("job_id must be a SHA-256 hex digest")
        with self._exclusive():
            self._validate_regular_file(self._facts_path)
            self._validate_regular_file(self._resume_path)
            facts_added = self._write_facts(output, job_id=job_id)
            resume_payload = self._render_resume(output)
            resume_written = resume_payload is not None
            if resume_payload is not None:
                self._validate_regular_file(self._resume_path)
                _atomic_write_bytes(self._resume_path, resume_payload)
                self._validate_regular_file(self._resume_path)
            return LocalSinkWriteResult(facts_added, resume_written)


__all__ = ["CodexLocalMemorySink", "LocalSinkWriteResult"]
