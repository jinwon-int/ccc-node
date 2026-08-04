"""Body-free identity contract for normalized asynchronous completions.

This module defines identity only.  It deliberately has no persistence,
delivery, provider adapter, or raw event payload surface.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import unicodedata
from typing import Literal

from .provider_capabilities import SUPPORTED_PROVIDERS

ASYNC_COMPLETION_SCHEMA_VERSION = 1
ASYNC_COMPLETION_EVENT_VERSION = 1
MAX_ASYNC_COMPLETION_IDENTIFIER_BYTES = 128
MAX_SESSION_GENERATION = (1 << 63) - 1

AsyncCompletionProvider = Literal["claude", "codex", "crush", "piri"]

_IDENTIFIER_PUNCTUATION = frozenset("-._:@/")
_IDENTITY_DOMAIN = b"ccc-node:normalized-async-completion:v1\x00"


def _validate_version(name: str, value: object, supported: int) -> None:
    if type(value) is not int or value != supported:
        raise ValueError(f"unsupported async completion {name}")


def _validate_identifier(name: str, value: object) -> None:
    if type(value) is not str or not value:
        raise ValueError(f"async completion {name} is invalid")
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError(f"async completion {name} is non-canonical")
    if not _is_identifier_edge(value[0]) or not _is_identifier_edge(value[-1]):
        raise ValueError(f"async completion {name} is invalid")
    for character in value:
        category = unicodedata.category(character)
        if category[0] == "C":
            raise ValueError(f"async completion {name} contains a control character")
        if not (_is_identifier_character(category) or character in _IDENTIFIER_PUNCTUATION):
            raise ValueError(f"async completion {name} is malformed")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError(f"async completion {name} is malformed") from error
    if len(encoded) > MAX_ASYNC_COMPLETION_IDENTIFIER_BYTES:
        raise ValueError(f"async completion {name} is oversized")


def _is_identifier_edge(character: str) -> bool:
    return unicodedata.category(character)[0] in {"L", "N"}


def _is_identifier_character(category: str) -> bool:
    return category[0] in {"L", "M", "N"}


def _frame(label: str, value: str) -> bytes:
    encoded = value.encode("utf-8")
    return label.encode("ascii") + len(encoded).to_bytes(4, "big") + encoded


@dataclass(frozen=True, slots=True, repr=False, kw_only=True)
class NormalizedAsyncCompletionEvent:
    """Immutable, provider-neutral identity for one out-of-turn completion.

    Exactly one of ``turn_id`` and ``task_id`` identifies the completion.  The
    object cannot carry bodies or arbitrary metadata, and its repr redacts the
    opaque identifiers.
    """

    provider: AsyncCompletionProvider
    thread_id: str = field(repr=False)
    conversation_route_id: str = field(repr=False)
    session_generation: int
    turn_id: str | None = field(default=None, repr=False)
    task_id: str | None = field(default=None, repr=False)
    schema_version: int = ASYNC_COMPLETION_SCHEMA_VERSION
    event_version: int = ASYNC_COMPLETION_EVENT_VERSION

    def __post_init__(self) -> None:
        _validate_version("schema version", self.schema_version, ASYNC_COMPLETION_SCHEMA_VERSION)
        _validate_version("event version", self.event_version, ASYNC_COMPLETION_EVENT_VERSION)
        if type(self.provider) is not str or self.provider not in SUPPORTED_PROVIDERS:
            raise ValueError("unsupported async completion provider")
        if (
            type(self.session_generation) is not int
            or self.session_generation <= 0
            or self.session_generation > MAX_SESSION_GENERATION
        ):
            raise ValueError("async completion session generation is invalid")
        if (self.turn_id is None) == (self.task_id is None):
            raise ValueError("async completion turn/task identity is ambiguous")

        _validate_identifier("thread id", self.thread_id)
        _validate_identifier("conversation route id", self.conversation_route_id)
        if self.turn_id is not None:
            _validate_identifier("turn id", self.turn_id)
        if self.task_id is not None:
            _validate_identifier("task id", self.task_id)

    @property
    def identity_hash(self) -> str:
        """Return the deterministic, body-free SHA-256 identity digest."""

        identity_kind = "turn" if self.turn_id is not None else "task"
        identity_value = self.turn_id if self.turn_id is not None else self.task_id
        assert identity_value is not None
        material = b"".join(
            (
                _IDENTITY_DOMAIN,
                _frame("schema-version", str(self.schema_version)),
                _frame("event-version", str(self.event_version)),
                _frame("provider", self.provider),
                _frame("thread-id", self.thread_id),
                _frame("identity-kind", identity_kind),
                _frame("identity-id", identity_value),
                _frame("conversation-route-id", self.conversation_route_id),
                _frame("session-generation", str(self.session_generation)),
            )
        )
        return hashlib.sha256(material).hexdigest()

    @property
    def idempotency_key(self) -> str:
        """Return a safe key containing no raw identifiers or event bodies."""

        return f"async-completion:{self.identity_hash}"

    def __repr__(self) -> str:
        identity_kind = "turn" if self.turn_id is not None else "task"
        return (
            f"{type(self).__name__}(schema_version={self.schema_version!r}, "
            f"event_version={self.event_version!r}, provider={self.provider!r}, "
            f"session_generation={self.session_generation!r}, "
            f"identity_kind={identity_kind!r}, idempotency_key={self.idempotency_key!r})"
        )
