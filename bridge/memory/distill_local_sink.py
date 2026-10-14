"""Replay-safe owner-only local write-back for validated Codex distill output."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
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
    ensure_private_directory,
)

from .distill_extraction import DistillExtractionOutput
from .local_memory_transaction import LocalMemoryTransaction


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_WORD_RE = re.compile(r"[0-9a-z가-힣]+")
_SUPPORTED_AUDIENCES = frozenset({"private", "shared"})
_MAX_EXISTING_FACT_BYTES = 8 * 1024 * 1024
# ``captured_at`` leads the header since #2301 so no budget truncation can cut
# it; headers written before that carry it last. Both orders are read, by a
# linear token split (no regex backtracking under the sink lock).
_RESUME_HEADER_PREFIX = "<!-- ccc-node:distill "
_RESUME_HEADER_SUFFIX = " -->"
_CAPTURED_AT_KEY = "captured_at="
_RESUME_TRUNCATION_SUFFIX = "\n… [truncated by CCC resume budget]\n"


@dataclass(frozen=True, slots=True)
class LocalSinkWriteResult:
    facts_added: int
    resume_written: bool


def _normalize_fact(text: str) -> str:
    return " ".join(_WORD_RE.findall(text.lower()))


def _single_line(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _bounded_utf8(text: str, max_bytes: int) -> bytes:
    """Bound ``text`` to ``max_bytes``; the result never exceeds the bound."""
    payload = text.encode("utf-8")
    if len(payload) <= max_bytes:
        return payload
    suffix = _RESUME_TRUNCATION_SUFFIX.encode()
    if max_bytes < len(suffix):
        return payload[: max(0, max_bytes)].decode("utf-8", errors="ignore").encode("utf-8")
    budget = max_bytes - len(suffix)
    prefix = payload[:budget].decode("utf-8", errors="ignore").encode("utf-8")
    return prefix + suffix


def _normalized_timestamp(value: str | None) -> str | None:
    """Render an aware ISO-8601 time as fixed-width UTC (27 chars), else None.

    ``distilled_at`` is model output and ``fromisoformat`` accepts any number of
    fractional digits, so the raw value is unbounded (#2301 L1).
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        # Edge offsets (0001-01-01T00:00+05:00) overflow on conversion.
        utc = parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None
    # strftime("%Y") does not zero-pad years < 1000 on glibc.
    return f"{utc.year:04d}" + utc.strftime("-%m-%dT%H:%M:%S.%fZ")


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
        resume_never_regresses: bool = False,
    ) -> None:
        if type(resume_never_regresses) is not bool:
            raise ValueError("resume_never_regresses must be boolean")
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
        # The node-global state (#2296) is shared with the hook path and may
        # receive replayed jobs in journal (hash) order: never let an older
        # capture replace a newer bridge resume.md. Ordering uses the
        # bridge-controlled snapshot ``captured_at`` recorded in the resume
        # header, never the model-authored ``distilled_at`` or file mtime.
        # Facts are still merged (they are deduplicated).
        self.resume_never_regresses = resume_never_regresses
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

    @staticmethod
    def _read_existing_facts(payload: bytes | None) -> tuple[list[str], set[str]]:
        if payload is None:
            return [], set()
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

    def _render_facts(
        self,
        output: DistillExtractionOutput,
        *,
        job_id: str,
        existing_payload: bytes | None,
    ) -> tuple[bytes | None, int]:
        lines, seen = self._read_existing_facts(existing_payload)
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
                # #871: durability follows the fact kind instead of a blanket
                # "durable" — task-progress ages like the index default expects.
                "durability": "volatile" if item.kind == "task-progress" else "durable",
                "confidence": 0.7,
                # #871 §4 / nunchi G2 — source precedence is a separate axis
                # from confidence, so a contradiction is never resolved by the
                # confidence number alone. Ranks mirror nunchi's peer_facts:
                # 3 user-stated, 2 measured, 1 inferred.
                #
                # Everything this sink writes is an agent's reading of a
                # transcript, and nothing here verifies a claim against a
                # literal quote. G2's rule for exactly that case is to demote
                # to 1 rather than trust a self-reported rank, so a distilled
                # fact can never close a user-stated or measured one. A writer
                # that CAN cite a quote sets a higher rank on its own facts.
                "source_rank": 1,
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
            return None, 0
        bounded_lines = (lines + added)[-self.max_facts :]
        payload = ("\n".join(bounded_lines) + "\n").encode("utf-8")
        return payload, len(added)

    def _render_resume(
        self,
        output: DistillExtractionOutput,
        *,
        captured_at: str | None = None,
    ) -> bytes | None:
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
        # Fields in priority order: captured_at first, so ordering survives
        # any budget, then provenance. Every value is bounded (enums, SHA-256,
        # normalized timestamps); the header itself is never truncated (#2301).
        fields: list[str] = []
        captured = _normalized_timestamp(captured_at)
        if captured is not None:
            fields.append(f"captured_at={captured}")
        fields += [
            f"schema={output.schema_version}",
            f"provider={provenance.provider}",
            f"thread_hash={provenance.source_thread_hash}",
            f"trigger={provenance.trigger.value}",
        ]
        distilled = _normalized_timestamp(provenance.distilled_at)
        if distilled is not None:
            fields.append(f"distilled_at={distilled}")
        keep = 1 if captured is not None else 0
        while True:
            header = _RESUME_HEADER_PREFIX + " ".join(fields) + _RESUME_HEADER_SUFFIX + "\n"
            if len(header.encode("utf-8")) <= self.max_resume_bytes or len(fields) <= keep:
                break
            fields.pop()
        header_bytes = header.encode("utf-8")
        body = _bounded_utf8(
            "\n".join(lines) + "\n", max(0, self.max_resume_bytes - len(header_bytes))
        )
        return header_bytes + body

    @staticmethod
    def _parse_capture_time(value: str | None) -> datetime | None:
        if not isinstance(value, str) or not value or len(value) > 64:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return None
            return parsed.astimezone(timezone.utc)
        except (ValueError, OverflowError):
            return None

    def _resume_mtime(self) -> datetime | None:
        try:
            stamp = os.stat(self._resume_path, follow_symlinks=False).st_mtime
        except OSError:
            return None
        return datetime.fromtimestamp(stamp, timezone.utc)

    @classmethod
    def _current_resume_captured_at(cls, payload: bytes | None) -> datetime | None:
        """The ``captured_at`` of the current resume.md header, if parseable."""
        if not payload:
            return None
        first_line = payload.split(b"\n", 1)[0].decode("utf-8", errors="replace")
        if not (
            first_line.startswith(_RESUME_HEADER_PREFIX)
            and first_line.endswith(_RESUME_HEADER_SUFFIX)
        ):
            return None
        fields = first_line[len(_RESUME_HEADER_PREFIX) : -len(_RESUME_HEADER_SUFFIX)]
        for token in fields.split(" "):
            if token.startswith(_CAPTURED_AT_KEY):
                return cls._parse_capture_time(token[len(_CAPTURED_AT_KEY) :])
        return None

    def write(
        self,
        output: DistillExtractionOutput,
        *,
        job_id: str,
        captured_at: str | None = None,
    ) -> LocalSinkWriteResult:
        """Merge facts and write resume.md.

        ``captured_at`` (the bridge-recorded snapshot time) only matters for a
        ``resume_never_regresses`` sink: it is stamped into the resume header
        and an extraction never replaces a current resume whose header
        carries a strictly later capture time, nor a resume without a stamped
        capture (hook-path CLI resume) whose file mtime is later (#2301 L3).
        """
        if not isinstance(output, DistillExtractionOutput):
            raise ValueError("output must be a validated DistillExtractionOutput")
        if not isinstance(job_id, str) or not _SHA256_RE.fullmatch(job_id):
            raise ValueError("job_id must be a SHA-256 hex digest")
        ordered = self.resume_never_regresses
        new_capture = self._parse_capture_time(captured_at) if ordered else None
        with self._exclusive():
            self._validate_regular_file(self._facts_path)
            self._validate_regular_file(self._resume_path)
            resume_payload = self._render_resume(
                output,
                captured_at=(
                    _normalized_timestamp(captured_at)
                    if new_capture is not None
                    else None
                ),
            )
            resume_written = resume_payload is not None
            facts_added = 0

            def transform(
                current: dict[str, bytes | None],
            ) -> dict[str, bytes | None]:
                nonlocal facts_added, resume_written
                desired: dict[str, bytes | None] = {}
                facts_payload, facts_added = self._render_facts(
                    output,
                    job_id=job_id,
                    existing_payload=current["memory-facts.jsonl"],
                )
                if facts_payload is not None:
                    desired["memory-facts.jsonl"] = facts_payload
                write_resume = resume_payload is not None
                if write_resume and ordered:
                    # Decided under the transaction lock against the CURRENT
                    # resume: a strictly newer stamped capture is kept.
                    current_capture = self._current_resume_captured_at(
                        current["resume.md"]
                    )
                    if current_capture is not None and (
                        new_capture is None or current_capture > new_capture
                    ):
                        write_resume = False
                    elif (
                        current_capture is None
                        and current["resume.md"]
                        and new_capture is not None
                    ):
                        # #2301 L3: no stamped capture — a hook-path CLI resume
                        # (header-less) or a pre-#2297 audience header. Its file
                        # mtime and the bridge capture are both host-clock times
                        # (never model output), so a replayed backlog older than
                        # the file must not replace it.
                        current_mtime = self._resume_mtime()
                        if current_mtime is not None and current_mtime > new_capture:
                            write_resume = False
                resume_written = write_resume
                if write_resume:
                    desired["resume.md"] = resume_payload
                return desired

            LocalMemoryTransaction(self.state_dir).commit(
                transform,
                provider=output.provenance.provider,
                actor="distill",
                tool="local-memory-sink",
                session=job_id,
                diff="mode-both",
            )
            return LocalSinkWriteResult(facts_added, resume_written)


__all__ = ["CodexLocalMemorySink", "LocalSinkWriteResult"]
