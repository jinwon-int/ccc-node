from __future__ import annotations

import logging
from dataclasses import fields
from typing import Any

from telegram_bot.core.agent_session_registry import AgentSessionRegistry
from telegram_bot.core.project_chat_types import AgentSessionEntry

# For type annotation support in tests
logging.getLogger(__name__)  # Silence unused import warning


def test_cached_slots_use_exact_tokens_and_monotonic_touch() -> None:
    registry = AgentSessionRegistry()
    key = (7, 70)
    first = AgentSessionEntry(session=object(), last_used_at=10.0)
    first_handle = registry.put_cached(key, first)

    assert registry.touch_cached_if_same(key, first_handle.token, 20.0)
    assert first.last_used_at == 20.0
    assert registry.touch_cached_if_same(key, first_handle.token, 15.0)
    assert first.last_used_at == 20.0

    second = AgentSessionEntry(session=object(), last_used_at=30.0)
    second_handle = registry.put_cached(key, second)
    assert second_handle.token != first_handle.token
    assert not registry.touch_cached_if_same(key, first_handle.token, 40.0)
    assert registry.drop_cached_if_same(key, first_handle.token) is None
    assert registry.get_cached(key) == second_handle
    assert registry.drop_cached_if_same(key, second_handle.token) is second
    assert registry.get_cached(key) is None


def test_active_tokens_separate_cleanup_ownership_from_approval_generation() -> None:
    registry = AgentSessionRegistry()
    key = (7, 70)
    session = object()
    registry.put_cached(
        key,
        AgentSessionEntry(session=session, last_used_at=1.0),
    )

    first = registry.register_active(key, session, started_at=2.0)
    assert registry.approval_is_active(key, first.generation)
    assert registry.active_started_at(key) == 2.0
    assert registry.metrics().waiting_for_turn == 1
    assert registry.admit_if_same(first)
    assert registry.metrics().waiting_for_turn == 0

    registry.invalidate_approvals((key,))
    assert not registry.approval_is_active(key, first.generation)
    assert registry.generation_high_water(key) == first.generation + 1
    assert registry.active_handle_if_same(first) is not None
    assert registry.deactivate_if_same(first, touch_at=3.0)

    second = registry.register_active(key, session, started_at=4.0)
    assert second.generation == first.generation + 2
    assert not registry.deactivate_if_same(first, touch_at=100.0)
    assert registry.active_handle_if_same(second) is not None
    assert registry.active_started_at(key) == 4.0
    assert registry.metrics().oldest_started_at == 4.0
    assert registry.deactivate_if_same(second, touch_at=5.0)
    assert registry.active_started_at(key) is None
    cached = registry.get_cached(key)
    assert cached is not None
    assert cached.entry.last_used_at == 5.0


def test_idle_lru_candidates_are_stable_and_rechecked_on_drop() -> None:
    registry = AgentSessionRegistry()
    entries = {
        (7, 70): AgentSessionEntry(session=object(), last_used_at=10.0),
        (7, 71): AgentSessionEntry(session=object(), last_used_at=10.0),
        (7, 72): AgentSessionEntry(session=object(), last_used_at=20.0),
    }
    for key, entry in entries.items():
        registry.put_cached(key, entry)

    active = registry.register_active((7, 71), entries[(7, 71)].session, started_at=30.0)
    candidates = registry.idle_lru_candidates()
    assert [candidate.key for candidate in candidates] == [(7, 70), (7, 72)]

    stale_after_activation = candidates[0]
    newly_active = registry.register_active(
        (7, 70),
        entries[(7, 70)].session,
        started_at=31.0,
    )
    assert registry.drop_idle_cached_if_same(stale_after_activation) is None
    assert registry.deactivate_if_same(newly_active)

    stale_after_replacement = candidates[1]
    replacement = AgentSessionEntry(session=object(), last_used_at=40.0)
    registry.put_cached((7, 72), replacement)
    assert registry.drop_idle_cached_if_same(stale_after_replacement) is None
    assert registry.get_cached((7, 72)) is not None
    assert registry.deactivate_if_same(active)


def test_generation_only_keys_do_not_pollute_live_metrics_or_ownership() -> None:
    registry = AgentSessionRegistry()
    for index in range(1_000):
        key = (index, index)
        token = registry.register_active(key, object(), started_at=float(index))
        assert registry.deactivate_if_same(token)

    metrics = registry.metrics()
    assert metrics.resident_sessions == 0
    assert metrics.active_sessions == 0
    assert metrics.waiting_for_turn == 0
    assert metrics.oldest_started_at is None
    assert registry.idle_lru_candidates() == ()
    assert not registry.has_live_owner((999, 999))
    assert registry.generation_high_water((999, 999)) == 1
    assert {field.name for field in fields(metrics)} == {
        "resident_sessions",
        "active_sessions",
        "waiting_for_turn",
        "oldest_started_at",
    }


def test_live_ownership_means_cached_or_active_not_generation_only() -> None:
    registry = AgentSessionRegistry()
    key = (7, 70)
    cached = registry.put_cached(key, AgentSessionEntry(session=object()))
    assert registry.has_live_owner(key)
    assert registry.drop_cached_if_same(key, cached.token) is not None
    assert not registry.has_live_owner(key)

    active = registry.register_active(key, object(), started_at=1.0)
    assert registry.has_live_owner(key)
    assert registry.deactivate_if_same(active)
    assert not registry.has_live_owner(key)
    assert registry.generation_high_water(key) == active.generation


def test_idle_clear_preserves_generation_and_refuses_active_owner() -> None:
    registry = AgentSessionRegistry()
    key = (7, 70)
    session = object()
    registry.put_cached(key, AgentSessionEntry(session=session))
    token = registry.register_active(key, session, started_at=1.0)

    assert registry.clear_cached_if_idle() is None
    assert registry.get_cached(key) is not None
    assert registry.deactivate_if_same(token)
    assert len(registry.clear_cached_if_idle() or ()) == 1
    assert registry.get_cached(key) is None
    assert registry.generation_high_water(key) == token.generation


def test_terminal_drain_snapshot_revokes_approval_without_rebinding_owner() -> None:
    registry = AgentSessionRegistry()
    key = (7, 70)
    session = object()
    token = registry.register_active(key, session, started_at=1.0)

    handles = registry.prepare_close()

    assert len(handles) == 1
    assert handles[0].token == token
    assert handles[0].session is session
    assert not registry.approval_is_active(key, token.generation)
    # Teardown still owns the exact active handle; a future turn cannot acquire
    # this generation or inherit its revoked approval route.
    assert registry.active_handle_if_same(token) == handles[0]


def test_register_active_race_condition_logs_warning(caplog: Any) -> None:
    """Per #860: race condition between turns emits WARN with diagnostics."""

    registry = AgentSessionRegistry()
    key = (7, 70)  # user_id, chat_id
    session = object()

    # Register first turn
    registry.register_active(key, session, started_at=1.0)

    # Attempt to register second turn while first is still active
    import pytest
    with pytest.raises(RuntimeError, match="already active"):
        registry.register_active(key, object(), started_at=2.0)

    # Verify warning was logged with diagnostic details
    assert len(caplog.records) == 1
    log_message = caplog.records[0].message
    assert "Request lifecycle race detected" in log_message
    assert "user=7" in log_message
    assert "chat=70" in log_message
    assert "existing_token=" in log_message
    assert "started_at=" in log_message


def test_deactivate_token_mismatch_logs_warning(caplog: Any) -> None:
    """Per #860: deactivate with wrong token logs warning."""
    import logging

    # Enable caplog for WARNING level
    caplog.set_level(logging.WARNING)

    registry = AgentSessionRegistry()
    key = (7, 70)
    session = object()

    token = registry.register_active(key, session, started_at=1.0)

    # Create wrong token by using different generation
    from telegram_bot.core.agent_session_registry import ActiveToken
    wrong_token = ActiveToken(key, token.generation + 1)

    result = registry.deactivate_if_same(wrong_token)

    # Verify deactivate failed
    assert result is False

    # Verify warning was logged
    assert len(caplog.records) >= 1
    log_message = caplog.records[-1].message
    assert "Deactivate failed: token mismatch" in log_message
    assert "user=7" in log_message


def test_deactivate_mismatch_warning_carries_finite_age() -> None:
    """The mismatch diagnostic's age must come from the started_at clock.

    started_at is recorded with loop.time() (monotonic); the old wall-clock
    subtraction printed ~1.7e9-second garbage ages, and the dead wall-clock
    force_cleanup_stale_turns helper built on the same mixing was removed.
    """
    import asyncio
    import logging as _logging

    from telegram_bot.core.agent_session_registry import ActiveToken

    async def scenario() -> str:
        registry = AgentSessionRegistry()
        key = (7, 70)
        loop = asyncio.get_running_loop()
        registry.register_active(key, object(), started_at=loop.time() - 5.0)
        stale = ActiveToken(key=key, generation=999)
        records: list[_logging.LogRecord] = []

        handler = _logging.Handler()
        handler.emit = records.append  # type: ignore[method-assign]
        logger = _logging.getLogger("telegram_bot.core.agent_session_registry")
        logger.addHandler(handler)
        try:
            assert registry.deactivate_if_same(stale) is False
        finally:
            logger.removeHandler(handler)
        return next(
            r.getMessage() for r in records if "token mismatch" in r.getMessage()
        )

    message = asyncio.run(scenario())
    age = float(message.split("active_age=")[1].split("s")[0])
    assert 0.0 <= age < 3600.0, message


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__]))


def test_find_route_by_session_id_is_fail_closed() -> None:
    """#646: reverse route attribution only for resident cached sessions."""

    from telegram_bot.core.agent_session_registry import AgentSessionRegistry
    from telegram_bot.core.project_chat_types import AgentSessionEntry

    class _Session:
        def __init__(self, session_id: str) -> None:
            self.session_id = session_id

    registry = AgentSessionRegistry()
    assert registry.find_route_by_session_id("thread-1") is None
    assert registry.find_route_by_session_id("") is None

    registry.put_cached((7, 42), AgentSessionEntry(session=_Session("thread-1")))
    registry.put_cached((8, 43), AgentSessionEntry(session=_Session("thread-2")))

    assert registry.find_route_by_session_id("thread-2") == (8, 43)
    assert registry.find_route_by_session_id("thread-1") == (7, 42)
    assert registry.find_route_by_session_id("thread-missing") is None

    # An entry whose session lacks a session_id attribute never matches.
    registry.put_cached((9, 44), AgentSessionEntry(session=object()))
    assert registry.find_route_by_session_id("thread-2") == (8, 43)


def test_generation_high_water_defaults_to_zero() -> None:
    from telegram_bot.core.agent_session_registry import AgentSessionRegistry

    registry = AgentSessionRegistry()
    assert registry.generation_high_water((7, 42)) == 0
