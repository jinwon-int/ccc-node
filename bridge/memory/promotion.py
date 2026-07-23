"""Audited, idempotent private-to-shared local memory promotion."""

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
from typing import Callable, Iterator

from telegram_bot.utils.secure_fs import (
    _atomic_write_bytes,
    ensure_private_directory,
)

from .distill_extraction import HonchoFact
from .distill_types import DistillTrigger, validate_memory_route


_FACT_ID_RE = re.compile(r"^distill-[0-9a-f]{12}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_FACT_BYTES = 8 * 1024 * 1024
_MAX_AUDIT_BYTES = 4 * 1024 * 1024


class PromotionFactNotFoundError(LookupError):
    """The requested fact does not exist in the caller's private scope."""


@dataclass(frozen=True, slots=True)
class MemoryPromotionResult:
    promotion_id: str
    destination_fact_id: str
    source_scope_hash: str
    source_fact_hash: str
    promoted: bool


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _timestamp(now: Callable[[], datetime]) -> str:
    value = now()
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("promotion clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _validated_timestamp(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field} timestamp is invalid") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{field} timestamp is invalid")
    return value


class CodexMemoryPromoter:
    """Copy one validated private local fact into shared storage on command.

    The caller supplies only an already-resolved opaque private scope and a
    local sink fact id. The audit intentionally excludes the fact body and the
    raw scope. Stable ids make concurrent/replayed commands converge.
    """

    def __init__(
        self,
        audience_root: Path,
        *,
        max_facts: int = 1000,
        max_audits: int = 4000,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if type(max_facts) is not int or max_facts <= 0:
            raise ValueError("max_facts must be a positive integer")
        if type(max_audits) is not int or max_audits <= 0:
            raise ValueError("max_audits must be a positive integer")
        self._audience_root = Path(
            os.path.abspath(os.fspath(audience_root))
        )
        self._shared_state = self._audience_root / "shared" / "state"
        self._shared_facts = self._shared_state / "memory-facts.jsonl"
        self._audit_path = self._shared_state / "memory-promotion-audit.jsonl"
        # Coordinate with CodexLocalMemorySink shared writes, not just other
        # promotion instances, so neither path can lose the other's append.
        self._lock_path = self._shared_state / ".local-memory-sink.lock"
        self._max_facts = max_facts
        self._max_audits = max_audits
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._thread_lock = threading.RLock()

    @staticmethod
    def _validate_regular_file(path: Path) -> None:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise PermissionError(f"memory promotion state must be regular: {path}")
        if metadata.st_nlink != 1:
            raise PermissionError("memory promotion state must not have hard links")
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise PermissionError("memory promotion state has the wrong owner")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise PermissionError("memory promotion state must have mode 0600")

    @classmethod
    def _validate_open_lock(cls, descriptor: int, path: Path) -> None:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise PermissionError(f"memory promotion lock must be regular: {path}")
        if metadata.st_nlink != 1:
            raise PermissionError("memory promotion lock must not have hard links")
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise PermissionError("memory promotion lock has the wrong owner")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise PermissionError("memory promotion lock must have mode 0600")

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        ensure_private_directory(self._shared_state)
        with self._thread_lock:
            self._validate_regular_file(self._lock_path)
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(self._lock_path, flags, 0o600)
            try:
                os.fchmod(descriptor, 0o600)
                self._validate_open_lock(descriptor, self._lock_path)
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                yield
            finally:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)

    @classmethod
    def _read_lines(cls, path: Path, *, max_bytes: int) -> list[str]:
        cls._validate_regular_file(path)
        try:
            payload = path.read_bytes()
        except FileNotFoundError:
            return []
        if len(payload) > max_bytes:
            raise ValueError("memory promotion state exceeds its safe read bound")
        return [line for line in payload.decode("utf-8").splitlines() if line.strip()]

    @staticmethod
    def _parse_object(line: str, *, state_name: str) -> dict[str, object]:
        try:
            value = json.loads(line)
        except (TypeError, ValueError) as error:
            raise ValueError(f"invalid {state_name} JSON") from error
        if not isinstance(value, dict):
            raise ValueError(f"invalid {state_name} record")
        return value

    @staticmethod
    def _validate_source_fact(
        value: dict[str, object],
        *,
        fact_id: str,
    ) -> dict[str, object]:
        if (
            value.get("schema_version") != 1
            or value.get("id") != fact_id
            or value.get("review") != "auto-local"
            or value.get("privacy") != "private"
            or value.get("audience") != "private"
            or value.get("durability") != "durable"
        ):
            raise ValueError("fact is not an eligible private local fact")
        confidence = value.get("confidence")
        if (
            not isinstance(confidence, (int, float))
            or isinstance(confidence, bool)
            or not 0 <= float(confidence) <= 1
        ):
            raise ValueError("private local fact confidence is invalid")
        entities = value.get("entities")
        if (
            not isinstance(entities, list)
            or len(entities) != 1
            or entities[0] not in {"user", "session", "node"}
        ):
            raise ValueError("private local fact entities are invalid")
        validated = HonchoFact.model_validate(
            {
                "kind": value.get("kind"),
                "text": value.get("text"),
                "subject": entities[0],
            }
        )
        source = value.get("source")
        if not isinstance(source, dict):
            raise ValueError("private local fact source is invalid")
        trigger = source.get("trigger")
        try:
            DistillTrigger(trigger)
        except (TypeError, ValueError) as error:
            raise ValueError("private local fact trigger is invalid") from error
        if (
            source.get("type") != "distill"
            or source.get("provider") != "codex"
            or source.get("schema_version") != 1
            or not isinstance(source.get("job_id"), str)
            or _SHA256_RE.fullmatch(str(source["job_id"])) is None
            or not isinstance(source.get("thread_hash"), str)
            or _SHA256_RE.fullmatch(str(source["thread_hash"])) is None
        ):
            raise ValueError("private local fact source is invalid")
        observed_at = _validated_timestamp(
            value.get("observed_at"),
            field="private local fact",
        )
        tags = value.get("tags")
        if (
            not isinstance(tags, list)
            or len(tags) > 14
            or any(
                not isinstance(tag, str)
                or not tag
                or len(tag) > 64
                or len(tag.encode("utf-8")) > 64
                for tag in tags
            )
        ):
            raise ValueError("private local fact tags are invalid")
        return {
            "schema_version": 1,
            "kind": validated.kind,
            "text": validated.text,
            "durability": "durable",
            "confidence": float(confidence),
            "observed_at": observed_at,
            "entities": list(entities),
            "tags": list(tags),
        }

    def _load_source(self, *, source_scope: str, fact_id: str) -> dict[str, object]:
        validate_memory_route("private", source_scope)
        if not _FACT_ID_RE.fullmatch(fact_id):
            raise ValueError("fact id must match distill-<12 lowercase hex>")
        source_state = self._audience_root / source_scope / "state"
        if source_state.parent.parent != self._audience_root:
            raise PermissionError("private memory scope escaped its audience root")
        ensure_private_directory(source_state)
        source_path = source_state / "memory-facts.jsonl"
        lines = self._read_lines(source_path, max_bytes=_MAX_FACT_BYTES)
        matches = [
            value
            for line in lines
            if (value := self._parse_object(line, state_name="private fact")).get("id")
            == fact_id
        ]
        if not matches:
            raise PromotionFactNotFoundError("private memory fact not found")
        if len(matches) != 1:
            raise ValueError("private memory fact id is not unique")
        source = matches[0]
        self._validate_source_fact(source, fact_id=fact_id)
        return source

    @staticmethod
    def _matching_records(
        lines: list[str],
        *,
        record_id: str,
        state_name: str,
    ) -> list[dict[str, object]]:
        matches: list[dict[str, object]] = []
        for line in lines:
            value = CodexMemoryPromoter._parse_object(line, state_name=state_name)
            if value.get("id") == record_id:
                matches.append(value)
        if len(matches) > 1:
            raise ValueError(f"duplicate {state_name} id")
        return matches

    @staticmethod
    def _destination_record(
        source_fact: dict[str, object],
        *,
        destination_fact_id: str,
        promotion_id: str,
        source_scope_hash: str,
        source_fact_hash: str,
        completed_at: str,
    ) -> dict[str, object]:
        safe = CodexMemoryPromoter._validate_source_fact(
            source_fact,
            fact_id=str(source_fact.get("id")),
        )
        raw_tags = safe["tags"]
        if not isinstance(raw_tags, list) or any(
            not isinstance(tag, str) for tag in raw_tags
        ):
            raise ValueError("validated private local fact tags are invalid")
        tags = [tag for tag in raw_tags if isinstance(tag, str)]
        for tag in ("promoted", "private-to-shared"):
            if tag not in tags:
                tags.append(tag)
        return {
            **safe,
            "id": destination_fact_id,
            "review": "explicit-promotion",
            "privacy": "shared",
            "audience": "shared",
            "tags": tags,
            "promoted_at": completed_at,
            "source": {
                "type": "private-to-shared-promotion",
                "promotion_id": promotion_id,
                "source_fact_id": source_fact["id"],
                "source_scope_hash": source_scope_hash,
                "source_fact_hash": source_fact_hash,
            },
        }

    @staticmethod
    def _audit_record(
        *,
        promotion_id: str,
        destination_fact_id: str,
        source_fact_id: str,
        source_scope_hash: str,
        source_fact_hash: str,
        completed_at: str,
    ) -> dict[str, object]:
        return {
            "schema_version": 1,
            "id": promotion_id,
            "action": "private-to-shared",
            "status": "completed",
            "requested_via": "authorized-telegram-command",
            "source": {
                "audience": "private",
                "scope_hash": source_scope_hash,
                "fact_id": source_fact_id,
                "fact_hash": source_fact_hash,
            },
            "destination": {
                "audience": "shared",
                "scope": "shared",
                "fact_id": destination_fact_id,
            },
            "completed_at": completed_at,
        }

    @classmethod
    def _append_bounded(
        cls,
        path: Path,
        lines: list[str],
        value: dict[str, object],
        *,
        limit: int,
    ) -> None:
        bounded = (lines + [_canonical_json(value)])[-limit:]
        cls._validate_regular_file(path)
        _atomic_write_bytes(path, ("\n".join(bounded) + "\n").encode("utf-8"))
        cls._validate_regular_file(path)

    def promote(
        self,
        *,
        source_scope: str,
        fact_id: str,
    ) -> MemoryPromotionResult:
        source_fact = self._load_source(
            source_scope=source_scope,
            fact_id=fact_id,
        )
        canonical_source = _canonical_json(source_fact)
        source_fact_hash = hashlib.sha256(canonical_source.encode("utf-8")).hexdigest()
        source_scope_hash = hashlib.sha256(source_scope.encode("utf-8")).hexdigest()
        stable = hashlib.sha256(
            f"ccc-memory-promotion-v1\0{source_scope}\0{fact_id}".encode()
        ).hexdigest()
        promotion_id = "promotion-" + stable[:24]
        destination_fact_id = "promoted-" + stable[:16]

        with self._exclusive():
            fact_lines = self._read_lines(
                self._shared_facts,
                max_bytes=_MAX_FACT_BYTES,
            )
            audit_lines = self._read_lines(
                self._audit_path,
                max_bytes=_MAX_AUDIT_BYTES,
            )
            existing_facts = self._matching_records(
                fact_lines,
                record_id=destination_fact_id,
                state_name="shared fact",
            )
            existing_audits = self._matching_records(
                audit_lines,
                record_id=promotion_id,
                state_name="promotion audit",
            )
            completed_at: str
            if existing_facts:
                completed_at = _validated_timestamp(
                    existing_facts[0].get("promoted_at"),
                    field="existing promoted fact",
                )
            elif existing_audits:
                completed_at = _validated_timestamp(
                    existing_audits[0].get("completed_at"),
                    field="existing promotion audit",
                )
            else:
                completed_at = _timestamp(self._now)

            destination = self._destination_record(
                source_fact,
                destination_fact_id=destination_fact_id,
                promotion_id=promotion_id,
                source_scope_hash=source_scope_hash,
                source_fact_hash=source_fact_hash,
                completed_at=completed_at,
            )
            audit = self._audit_record(
                promotion_id=promotion_id,
                destination_fact_id=destination_fact_id,
                source_fact_id=fact_id,
                source_scope_hash=source_scope_hash,
                source_fact_hash=source_fact_hash,
                completed_at=completed_at,
            )
            if existing_facts and existing_facts[0] != destination:
                raise ValueError("existing promoted fact does not match its source")
            if existing_audits and existing_audits[0] != audit:
                raise ValueError("existing promotion audit does not match its source")

            first_promotion = not existing_facts and not existing_audits
            if not existing_audits and len(audit_lines) >= self._max_audits:
                raise ValueError(
                    "memory promotion audit capacity requires operator rotation"
                )
            if not existing_facts:
                self._append_bounded(
                    self._shared_facts,
                    fact_lines,
                    destination,
                    limit=self._max_facts,
                )
            if not existing_audits:
                self._append_bounded(
                    self._audit_path,
                    audit_lines,
                    audit,
                    limit=self._max_audits,
                )

        return MemoryPromotionResult(
            promotion_id=promotion_id,
            destination_fact_id=destination_fact_id,
            source_scope_hash=source_scope_hash,
            source_fact_hash=source_fact_hash,
            promoted=first_promotion,
        )


__all__ = [
    "CodexMemoryPromoter",
    "MemoryPromotionResult",
    "PromotionFactNotFoundError",
]
