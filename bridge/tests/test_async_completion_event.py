"""Focused tests for the body-free asynchronous completion identity contract."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields
import re
from typing import Any, get_args

import pytest

from telegram_bot.core.async_completion_event import (
    ASYNC_COMPLETION_EVENT_VERSION,
    ASYNC_COMPLETION_SCHEMA_VERSION,
    MAX_ASYNC_COMPLETION_IDENTIFIER_BYTES,
    MAX_SESSION_GENERATION,
    AsyncCompletionProvider,
    NormalizedAsyncCompletionEvent,
)
from telegram_bot.core.provider_capabilities import SUPPORTED_PROVIDERS


class _EncodeTrap(str):
    def encode(self, *args: Any, **kwargs: Any) -> bytes:
        raise AssertionError("hostile str subclass encode must not run")


class _EqualityTrap(str):
    def __eq__(self, other: object) -> bool:
        raise AssertionError("hostile str subclass equality must not run")

    __hash__ = str.__hash__


def _event(**changes: Any) -> NormalizedAsyncCompletionEvent:
    values: dict[str, Any] = {
        "provider": "codex",
        "thread_id": "thread-123",
        "turn_id": "turn-456",
        "conversation_route_id": "telegram:route-789",
        "session_generation": 3,
    }
    values.update(changes)
    return NormalizedAsyncCompletionEvent(**values)


def test_valid_turn_and_task_construction() -> None:
    turn = _event()
    task = _event(
        provider="claude",
        turn_id=None,
        task_id="task-456",
        session_generation=MAX_SESSION_GENERATION,
    )

    assert turn.schema_version == ASYNC_COMPLETION_SCHEMA_VERSION
    assert turn.event_version == ASYNC_COMPLETION_EVENT_VERSION
    assert turn.turn_id == "turn-456"
    assert turn.task_id is None
    assert task.task_id == "task-456"
    assert task.turn_id is None
    assert task.session_generation == MAX_SESSION_GENERATION


def test_constructor_is_keyword_only() -> None:
    constructor: Any = NormalizedAsyncCompletionEvent

    with pytest.raises(TypeError, match="positional argument"):
        constructor("codex", "thread-123", "telegram:route-789", 3, "turn-456")


def test_provider_literal_matches_authoritative_supported_providers() -> None:
    assert get_args(AsyncCompletionProvider) == SUPPORTED_PROVIDERS


def test_equality_hashing_and_immutability() -> None:
    first = _event()
    second = _event()

    assert first == second
    assert hash(first) == hash(second)
    assert not hasattr(first, "__dict__")
    with pytest.raises(FrozenInstanceError):
        first.thread_id = "changed"  # type: ignore[misc]
    with pytest.raises((AttributeError, TypeError)):
        object.__setattr__(first, "payload", "secret")


def test_stable_body_free_identity() -> None:
    event = _event()
    identity_hash = event.identity_hash

    assert identity_hash == "09c8dd9541aae9ad7ba6d193d3496d0bf4a7a265f9e7a73202d8aa8d04fe03ec"
    assert {event.identity_hash for _ in range(10)} == {identity_hash}
    assert event.idempotency_key == f"async-completion:{identity_hash}"
    assert re.fullmatch(r"async-completion:[0-9a-f]{64}", event.idempotency_key)
    for raw_identifier in (event.thread_id, event.turn_id, event.conversation_route_id):
        assert raw_identifier not in event.idempotency_key


@pytest.mark.parametrize("hostile_type", (_EncodeTrap, _EqualityTrap))
def test_rejects_hostile_provider_str_subclasses(hostile_type: type[str]) -> None:
    with pytest.raises(ValueError, match="unsupported.*provider"):
        _event(provider=hostile_type("codex"))


@pytest.mark.parametrize("hostile_type", (_EncodeTrap, _EqualityTrap))
@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("thread_id", "thread-123"),
        ("conversation_route_id", "telegram:route-789"),
        ("turn_id", "turn-456"),
        ("task_id", "task-456"),
    ),
)
def test_rejects_hostile_identifier_str_subclasses(
    hostile_type: type[str], field_name: str, value: str
) -> None:
    changes: dict[str, object] = {field_name: hostile_type(value)}
    if field_name == "task_id":
        changes["turn_id"] = None

    with pytest.raises(ValueError, match="invalid"):
        _event(**changes)


def test_field_framing_and_identity_kind_resist_concatenation_collisions() -> None:
    events = (
        _event(thread_id="a", conversation_route_id="bc"),
        _event(thread_id="ab", conversation_route_id="c"),
        _event(thread_id="telegram:route-789", conversation_route_id="thread-123"),
        _event(turn_id=None, task_id="turn-456"),
        _event(provider="claude"),
        _event(session_generation=4),
    )

    assert len({event.identity_hash for event in events}) == len(events)


def test_identifier_limit_is_measured_in_utf8_bytes() -> None:
    at_limit = "é" * (MAX_ASYNC_COMPLETION_IDENTIFIER_BYTES // 2)
    over_limit = at_limit + "é"

    assert len(at_limit.encode("utf-8")) == MAX_ASYNC_COMPLETION_IDENTIFIER_BYTES
    assert _event(thread_id=at_limit).thread_id == at_limit
    with pytest.raises(ValueError, match="oversized"):
        _event(thread_id=over_limit)


@pytest.mark.parametrize("name", ("schema_version", "event_version"))
@pytest.mark.parametrize("value", (0, 2, -1, True, 1.0, "1"))
def test_rejects_unsupported_or_malformed_versions(name: str, value: object) -> None:
    with pytest.raises(ValueError, match="unsupported"):
        _event(**{name: value})


@pytest.mark.parametrize("provider", ("", "Codex", "codex ", "unknown", None, 1))
def test_rejects_unsupported_or_noncanonical_providers(provider: object) -> None:
    with pytest.raises(ValueError, match="unsupported.*provider"):
        _event(provider=provider)


@pytest.mark.parametrize(
    "generation",
    (0, -1, MAX_SESSION_GENERATION + 1, True, 1.0, "1", None),
)
def test_rejects_invalid_session_generation(generation: object) -> None:
    with pytest.raises(ValueError, match="session generation"):
        _event(session_generation=generation)


@pytest.mark.parametrize(
    ("turn_id", "task_id"),
    ((None, None), ("turn-456", "task-456")),
)
def test_rejects_ambiguous_turn_or_task_identity(
    turn_id: str | None, task_id: str | None
) -> None:
    with pytest.raises(ValueError, match="ambiguous"):
        _event(turn_id=turn_id, task_id=task_id)


@pytest.mark.parametrize(
    "value",
    (
        "",
        " leading",
        "trailing ",
        "internal space",
        "-leading",
        "trailing-",
        "not?allowed",
        "null\x00byte",
        "line\nfeed",
        "zero\u200bwidth",
        "private\ue000use",
        "e\u0301",
        "\ud800",
        "a" * (MAX_ASYNC_COMPLETION_IDENTIFIER_BYTES + 1),
        123,
        None,
    ),
)
@pytest.mark.parametrize(
    ("field_name", "changes"),
    (
        ("thread id", {"thread_id": None}),
        ("conversation route id", {"conversation_route_id": None}),
        ("turn id", {"turn_id": None}),
        ("task id", {"turn_id": None, "task_id": None}),
    ),
)
def test_rejects_every_invalid_identifier_boundary(
    field_name: str, changes: dict[str, object], value: object
) -> None:
    changes = dict(changes)
    target = {
        "thread id": "thread_id",
        "conversation route id": "conversation_route_id",
        "turn id": "turn_id",
        "task id": "task_id",
    }[field_name]
    changes[target] = value
    if target == "task_id":
        changes["turn_id"] = None
    with pytest.raises(ValueError):
        _event(**changes)


def test_sensitive_body_fields_cannot_enter_object_or_repr() -> None:
    sensitive = "telegram body secret credential prompt response"
    allowed_fields = {field.name for field in fields(NormalizedAsyncCompletionEvent)}

    assert allowed_fields == {
        "schema_version",
        "event_version",
        "provider",
        "thread_id",
        "turn_id",
        "task_id",
        "conversation_route_id",
        "session_generation",
    }
    for forbidden in ("payload", "prompt", "response", "telegram_body", "credentials", "metadata"):
        with pytest.raises(TypeError):
            _event(**{forbidden: sensitive})

    rendered = repr(
        _event(
            thread_id="telegram-body-secret",
            turn_id="prompt-response-secret",
            conversation_route_id="credential-secret",
        )
    )
    for secret_fragment in ("telegram-body", "prompt-response", "credential-secret"):
        assert secret_fragment not in rendered
