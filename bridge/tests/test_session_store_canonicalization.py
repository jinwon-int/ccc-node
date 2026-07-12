"""Regression test: the /resume selection list must be JSON-canonical.

``_cmd_resume`` used to store its selection list as
``[(session_id, message), ...]`` — a list of *tuples*. ``SessionStore``
deliberately rejects any value whose Python type does not survive a JSON
encode/decode round-trip (a tuple decodes back as a list), so the payload
raised ``SessionStoreValidationError: non-canonical JSON type ...
resume_list[0]: tuple`` and aborted the whole save, breaking session
persistence for that user.

These tests pin the fix at its source (the producer now emits lists) and keep
the strict validator honest as the guardrail that originally surfaced the bug.
"""

import asyncio
import sys
import tempfile
import types
from pathlib import Path

import pytest

BRIDGE_DIR = Path(__file__).resolve().parents[1]
if "telegram_bot" not in sys.modules:
    package = types.ModuleType("telegram_bot")
    package.__path__ = [str(BRIDGE_DIR)]
    sys.modules["telegram_bot"] = package

from telegram_bot.session.store import (  # noqa: E402
    SessionStore,
    SessionStoreValidationError,
    _validate_session_data,
)


def run(awaitable):
    return asyncio.run(awaitable)


def initialized_store(path: Path) -> SessionStore:
    store = SessionStore(path)
    store.initialize()
    return store


def _build_resume_list(sessions):
    """Mirror the fixed ``_cmd_resume`` comprehension (bot_commands.py)."""
    return [[sid, msg] for sid, msg, _ in sessions]


# The shape ``project_chat_handler.list_sessions()`` yields: (id, message, mtime).
SESSIONS = [("sid-1", "first summary", 1.0), ("sid-2", "second summary", 2.0)]


def test_fixed_producer_output_is_canonical():
    resume_list = _build_resume_list(SESSIONS)
    assert resume_list == [["sid-1", "first summary"], ["sid-2", "second summary"]]
    # Validates cleanly under the store's strict schema (no exception).
    _validate_session_data({"telegram_session:1": {"resume_list": resume_list}})


def test_resume_list_persists_and_reloads_through_store():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "sessions.json"
        resume_list = _build_resume_list(SESSIONS)

        store = initialized_store(path)
        run(store.update(4242, {"resume_list": resume_list}))

        stored = run(store.get(4242))
        assert stored["resume_list"] == resume_list

        # Survives a cold reload from disk unchanged...
        reloaded = run(initialized_store(path).get(4242))
        assert reloaded["resume_list"] == resume_list

        # ...and the delivery-side unpacking (``sid, msg = resume_list[idx]``)
        # still works on the two-element lists.
        sid, msg = reloaded["resume_list"][0]
        assert (sid, msg) == ("sid-1", "first summary")


def test_tuple_resume_list_is_rejected_by_validator():
    """Guardrail: reintroducing tuples must fail loudly, not persist silently."""
    tuple_form = [(sid, msg) for sid, msg, _ in SESSIONS]
    try:
        _validate_session_data({"telegram_session:1": {"resume_list": tuple_form}})
    except SessionStoreValidationError as error:
        assert "resume_list[0]" in str(error)
        assert "tuple" in str(error)
    else:  # pragma: no cover - protects against a silent regression
        raise AssertionError("tuple payload was unexpectedly accepted")


@pytest.mark.parametrize("provider", [[], {}, 1, True])
def test_provider_validation_rejects_non_string_json_values(provider) -> None:
    with pytest.raises(SessionStoreValidationError, match="invalid provider"):
        _validate_session_data({"telegram_session:1": {"provider": provider}})
