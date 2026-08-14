"""Unit tests for the durable external-wait registry and route binding (#740)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from telegram_bot.core.external_wait import (
    TERMINAL_EXPIRED,
    TERMINAL_OWNER_CANCEL,
    TERMINAL_SUCCESS,
    ExternalWaitRegistry,
    ExternalWaitValidationError,
    clear_active_turn,
    default_active_turns_path,
    default_registry_path,
    publish_active_turn,
    resolve_active_route,
    validate_head_sha,
    validate_pr_number,
    validate_repo,
    validate_summary,
)


def _register(registry: ExternalWaitRegistry, **overrides) -> str:
    params = {
        "repo": "jinwon-int/ccc-node",
        "pr_number": 123,
        "head_sha": "abc1234",
        "user_id": 7,
        "chat_id": 70,
        "session_id": "sess-1",
        "summary": "merge when green",
        "timeout_seconds": 600,
        "poll_interval_seconds": 30,
        "now": 1_000.0,
    }
    params.update(overrides)
    return registry.register(**params)


def test_validation_is_bounded_and_body_free() -> None:
    with pytest.raises(ExternalWaitValidationError):
        validate_repo("not-a-repo")
    with pytest.raises(ExternalWaitValidationError):
        validate_pr_number(0)
    with pytest.raises(ExternalWaitValidationError):
        validate_pr_number("abc")
    with pytest.raises(ExternalWaitValidationError):
        validate_head_sha("not hex!")
    assert validate_repo(" a/b ") == "a/b"
    assert validate_pr_number("42") == 42
    assert validate_head_sha("ABC1234") == "abc1234"
    assert validate_summary("  many\n\nspaces   here ") == "many spaces here"
    assert len(validate_summary("x" * 500)) == 200


def test_register_is_idempotent_per_natural_key(tmp_path: Path) -> None:
    registry = ExternalWaitRegistry(default_registry_path(tmp_path))

    first = _register(registry)
    second = _register(registry)
    different_head = _register(registry, head_sha="def5678")

    assert first == second
    assert different_head != first
    assert len(registry.records()) == 2


def test_finish_journals_wake_and_first_write_wins(tmp_path: Path) -> None:
    registry = ExternalWaitRegistry(default_registry_path(tmp_path))
    wait_id = _register(registry)

    assert registry.finish(wait_id, TERMINAL_SUCCESS, now=1_100.0) is True
    assert registry.finish(wait_id, TERMINAL_EXPIRED, now=1_200.0) is False

    record = registry.get(wait_id)
    assert record["state"] == TERMINAL_SUCCESS
    assert record["terminal_status"] == TERMINAL_SUCCESS
    pending = registry.pending_wakes()
    assert [rec["wait_id"] for rec in pending] == [wait_id]


def test_wake_journal_retries_then_settles(tmp_path: Path) -> None:
    registry = ExternalWaitRegistry(default_registry_path(tmp_path))
    wait_id = _register(registry)
    registry.finish(wait_id, TERMINAL_SUCCESS, now=1_100.0)

    registry.mark_wake(wait_id, delivered=False)
    assert registry.pending_wakes()[0]["wake"]["attempts"] == 1
    registry.mark_wake(wait_id, delivered=True)
    assert registry.pending_wakes() == []
    assert registry.get(wait_id)["wake"]["state"] == "done"


def test_wake_journal_gives_up_after_bounded_attempts(tmp_path: Path) -> None:
    registry = ExternalWaitRegistry(default_registry_path(tmp_path))
    wait_id = _register(registry)
    registry.finish(wait_id, TERMINAL_SUCCESS, now=1_100.0)

    for _ in range(10):
        registry.mark_wake(wait_id, delivered=False)

    assert registry.pending_wakes() == []
    assert registry.get(wait_id)["wake"]["state"] == "failed"


def test_due_filters_monitoring_records_by_schedule(tmp_path: Path) -> None:
    registry = ExternalWaitRegistry(default_registry_path(tmp_path))
    ready = _register(registry)
    later = _register(registry, head_sha="fff0000")
    registry.reschedule(later, next_poll_epoch=5_000.0, poll_interval_seconds=60)

    due = registry.due(now=2_000.0)
    assert [rec["wait_id"] for rec in due] == [ready]

    registry.finish(ready, TERMINAL_SUCCESS, now=2_100.0)
    assert registry.due(now=9_999.0)[0]["wait_id"] == later


def test_cancel_is_idempotent_and_terminal(tmp_path: Path) -> None:
    registry = ExternalWaitRegistry(default_registry_path(tmp_path))
    wait_id = _register(registry)

    assert registry.cancel(wait_id, now=1_100.0) is True
    assert registry.cancel(wait_id, now=1_200.0) is False
    assert registry.get(wait_id)["state"] == TERMINAL_OWNER_CANCEL
    assert registry.finish(wait_id, TERMINAL_SUCCESS, now=1_300.0) is False


def test_reconcile_on_start_rearms_monitoring_and_keeps_wake_journal(
    tmp_path: Path,
) -> None:
    registry = ExternalWaitRegistry(default_registry_path(tmp_path))
    monitoring = _register(registry)
    finished = _register(registry, head_sha="0badf00d")
    registry.reschedule(monitoring, next_poll_epoch=99_999.0, poll_interval_seconds=90)
    registry.finish(finished, TERMINAL_SUCCESS, now=1_100.0)

    rearmed = registry.reconcile_on_start(now=2_000.0)

    assert rearmed == 1
    assert registry.get(monitoring)["next_poll_epoch"] == 2_000.0
    assert [rec["wait_id"] for rec in registry.pending_wakes()] == [finished]


def test_terminal_records_prune_after_retention(tmp_path: Path) -> None:
    registry = ExternalWaitRegistry(default_registry_path(tmp_path))
    wait_id = _register(registry)
    registry.finish(wait_id, TERMINAL_SUCCESS, now=1_100.0)
    registry.mark_wake(wait_id, delivered=True)

    _register(registry, head_sha="1234567", now=1_100.0 + 90_000.0)

    remaining = {rec["wait_id"] for rec in registry.records()}
    assert wait_id not in remaining


def test_corrupt_primary_recovers_from_backup(tmp_path: Path) -> None:
    path = default_registry_path(tmp_path)
    registry = ExternalWaitRegistry(path)
    wait_id = _register(registry)
    # A second durable write preserves the post-register state in .bak.
    registry.reschedule(wait_id, next_poll_epoch=2_000.0, poll_interval_seconds=60)

    path.write_text("{ not json", encoding="utf-8")

    recovered = ExternalWaitRegistry(path)
    assert recovered.get(wait_id) is not None


def test_active_route_single_fresh_entry_resolves(tmp_path: Path) -> None:
    publish_active_turn(tmp_path, user_id=7, chat_id=70, session_id="sess-1", now=1_000.0)

    route = resolve_active_route(tmp_path, now=1_100.0)

    assert route is not None
    assert route["user_id"] == 7 and route["chat_id"] == 70
    assert route["session_id"] == "sess-1"


def test_active_route_is_fail_closed_when_ambiguous_or_stale(tmp_path: Path) -> None:
    publish_active_turn(tmp_path, user_id=7, chat_id=70, session_id="a", now=1_000.0)
    publish_active_turn(tmp_path, user_id=8, chat_id=80, session_id="b", now=1_000.0)
    assert resolve_active_route(tmp_path, now=1_100.0) is None

    clear_active_turn(tmp_path, user_id=8, chat_id=80, session_id="b")
    assert resolve_active_route(tmp_path, now=1_100.0) is not None

    assert resolve_active_route(tmp_path, now=1_100.0 + 7 * 3600) is None


def test_clear_active_turn_only_removes_the_matching_session(tmp_path: Path) -> None:
    publish_active_turn(tmp_path, user_id=7, chat_id=70, session_id="old", now=1_000.0)
    publish_active_turn(tmp_path, user_id=7, chat_id=70, session_id="new", now=2_000.0)

    clear_active_turn(tmp_path, user_id=7, chat_id=70, session_id="old")

    route = resolve_active_route(tmp_path, now=2_100.0)
    assert route is not None and route["session_id"] == "new"


def test_active_turns_file_is_owner_only(tmp_path: Path) -> None:
    import stat

    publish_active_turn(tmp_path, user_id=7, chat_id=70, session_id="s", now=1_000.0)
    mode = stat.S_IMODE(default_active_turns_path(tmp_path).stat().st_mode)
    assert mode & 0o022 == 0


# --- provider-neutral turn wiring: publish inside, clear on exit --------------

from collections.abc import AsyncIterator  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from telegram_bot.core.agent_runtime import (  # noqa: E402
    AgentEvent,
    ApprovalHandler,
    CompletionEvent,
    SessionRequest,
    TextDeltaEvent,
    deny_approval,
)
from telegram_bot.core.project_chat import ProjectChatHandler  # noqa: E402


class _FakeSession:
    def __init__(self, session_id: str, home: Path) -> None:
        self.session_id = session_id
        self._home = home
        self.route_during_turn = None

    def send_turn(
        self,
        message: str,
        *,
        approval_handler: ApprovalHandler = deny_approval,
    ) -> AsyncIterator[AgentEvent]:
        async def stream() -> AsyncIterator[AgentEvent]:
            self.route_during_turn = resolve_active_route(self._home)
            yield TextDeltaEvent("ok")
            yield CompletionEvent("end_turn")

        return stream()

    async def interrupt(self) -> None:
        return None


class _BlockingRouteSession(_FakeSession):
    def __init__(self, session_id: str, home: Path) -> None:
        super().__init__(session_id, home)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    def send_turn(
        self,
        message: str,
        *,
        approval_handler: ApprovalHandler = deny_approval,
    ) -> AsyncIterator[AgentEvent]:
        async def stream() -> AsyncIterator[AgentEvent]:
            self.route_during_turn = resolve_active_route(self._home)
            self.started.set()
            await self.release.wait()
            yield TextDeltaEvent("ok")
            yield CompletionEvent("end_turn")

        return stream()


class _FakeRuntime:
    supports_session_browsing = False

    def __init__(self, session: _FakeSession) -> None:
        self.session = session

    async def start_or_resume(self, request: SessionRequest) -> _FakeSession:
        return self.session

    async def close(self) -> None:
        return None

    async def recycle(self) -> bool:
        return True


@pytest.mark.anyio
async def test_turn_publishes_route_and_clears_it_on_exit(tmp_path: Path) -> None:
    home = tmp_path / ".telegram_bot" / "external-wait"
    session = _FakeSession("sess-1", home)
    settings = SimpleNamespace(
        agent_provider="claude",
        project_root=tmp_path,
        execution_profile="strict-project",
        bash_policy="disabled",
        allowed_user_ids=[7],
        require_allowlist=True,
        claude_cli_path=None,
        claude_settings_path=tmp_path / "claude" / "settings.json",
        enable_streaming=False,
        enable_partial_streaming=False,
        bot_data_dir=None,
        task_ledger_path=None,
        session_guard_enabled=False,
    )
    handler = ProjectChatHandler(settings=settings, agent_runtime=_FakeRuntime(session))
    handler._task_ledger_cache = False

    response = await handler.process_message("hello", 7, 70)

    assert response.success is True
    # Inside the turn the CLI would bind exactly this conversation/session.
    assert session.route_during_turn is not None
    assert session.route_during_turn["user_id"] == 7
    assert session.route_during_turn["chat_id"] == 70
    assert session.route_during_turn["session_id"] == "sess-1"
    # After the turn the route is gone (fail-closed for late registrations).
    assert resolve_active_route(home) is None


@pytest.mark.anyio
async def test_external_wait_route_is_independent_of_approval_generation(
    tmp_path: Path,
) -> None:
    """#804: external-wait publication and approval leases are distinct contracts."""

    home = tmp_path / ".telegram_bot" / "external-wait"
    session = _BlockingRouteSession("sess-1", home)
    settings = SimpleNamespace(
        agent_provider="claude",
        project_root=tmp_path,
        execution_profile="strict-project",
        bash_policy="disabled",
        allowed_user_ids=[7],
        require_allowlist=True,
        claude_cli_path=None,
        claude_settings_path=tmp_path / "claude" / "settings.json",
        enable_streaming=False,
        enable_partial_streaming=False,
        bot_data_dir=None,
        task_ledger_path=None,
        session_guard_enabled=False,
    )
    handler = ProjectChatHandler(settings=settings, agent_runtime=_FakeRuntime(session))
    handler._task_ledger_cache = False

    turn = asyncio.create_task(handler.process_message("hello", 7, 70))
    await asyncio.wait_for(session.started.wait(), timeout=2.0)
    assert handler.is_agent_approval_active(7, 70, 1)
    assert resolve_active_route(home) is not None

    # Approval revocation (/stop, /new, provider switch, or terminal teardown)
    # does not silently rewrite external-wait's file-backed route contract.
    handler.invalidate_agent_approvals(7, 70)
    assert not handler.is_agent_approval_active(7, 70, 1)
    assert resolve_active_route(home) is not None

    session.release.set()
    response = await asyncio.wait_for(turn, timeout=2.0)
    assert response.success is True
    assert resolve_active_route(home) is None


def test_mark_delivery_failed_stamps_reason_and_ignores_unknown_wait(
    tmp_path: Path,
) -> None:
    """Delivery-failure journal (#1109): durable reason on the wait record."""
    registry = ExternalWaitRegistry(default_registry_path(tmp_path))
    assert registry.mark_delivery_failed("missing", "BadRequest") is False

    wait_id = _register(registry)
    assert registry.mark_delivery_failed(wait_id, "BadRequest") is True

    stored = registry._read()[wait_id]
    assert stored["delivery_failed"]["reason"] == "BadRequest"
    assert stored["delivery_failed"]["at"]
    # The monitoring contract is untouched: the stamp is an audit side-record.
    assert stored["state"] == "monitoring"
    assert stored["wake"] is None
