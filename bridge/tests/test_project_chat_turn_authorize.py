"""Focused tests for the authorization helper extracted in #896 PR5.

``_authorize_turn`` binds the recovery dispatch guard and the Danso
resume/follow-up authorizations to the session before the stream starts. The
inline code set ``resume_authorized`` / ``followup_authorized`` as locals at
two separate points and the turn's ``finally`` read whatever was current when
control left; the helper writes them into a ``_TurnAuthorization`` slot at
the same points. The test that matters is the last one: a failure between
the two grants must leave the first grant visible so ``_release_turn`` still
clears it.
"""

from __future__ import annotations

from typing import Any

import pytest

from telegram_bot.core.project_chat_process import (
    ProjectChatProcessMixin,
    _TurnAuthorization,
)
from telegram_bot.core.usage_meter import MODE_INTERACTIVE


class _Config:
    def __init__(self, provider: str = "claude") -> None:
        self.agent_provider = provider


class _Host:
    def __init__(self, provider: str = "claude") -> None:
        self._config = _Config(provider)


class _Session:
    """A session offering every hook; drop attributes to model older runtimes."""

    session_id = "sess-1"

    def __init__(self) -> None:
        self.guards: list[object] = []
        self.granted: list[str] = []

    def abort_stalled_turn(self) -> None:  # pragma: no cover - identity only
        pass

    def set_dispatch_guard(self, guard: object) -> None:
        self.guards.append(guard)

    def authorize_task_resume(self) -> None:
        self.granted.append("resume")

    def authorize_task_followup(self) -> None:
        self.granted.append("followup")


def _authorize(host: _Host, session: Any, *, dispatch_guard: Any = None,
               resume_task: bool = False, usage_mode: str = MODE_INTERACTIVE,
               authorization: _TurnAuthorization | None = None) -> tuple[Any, _TurnAuthorization]:
    slot = authorization or _TurnAuthorization()
    denied = ProjectChatProcessMixin._authorize_turn(
        host,
        session=session,
        dispatch_guard=dispatch_guard,
        resume_task=resume_task,
        usage_mode=usage_mode,
        authorization=slot,
    )
    return denied, slot


def test_plain_turn_grants_nothing_and_exposes_the_abort_hook() -> None:
    session = _Session()
    denied, slot = _authorize(_Host(), session)
    assert denied is None
    assert slot.resume_authorized is False
    assert slot.followup_authorized is False
    assert slot.abort_stalled_turn == session.abort_stalled_turn
    assert session.guards == [] and session.granted == []


def test_session_without_abort_hook_yields_none() -> None:
    session = _Session()
    del _Session.abort_stalled_turn
    try:
        _, slot = _authorize(_Host(), session)
    finally:
        _Session.abort_stalled_turn = lambda self: None  # type: ignore[method-assign]
    assert slot.abort_stalled_turn is None


def test_live_dispatch_guard_is_bound_to_the_session() -> None:
    session = _Session()
    guard = lambda: True  # noqa: E731
    denied, _ = _authorize(_Host(), session, dispatch_guard=guard)
    assert denied is None
    assert session.guards == [guard]


def test_expired_dispatch_guard_is_refused_before_any_grant() -> None:
    session = _Session()
    denied, slot = _authorize(_Host(), session, dispatch_guard=lambda: False,
                              resume_task=True)
    assert denied is not None and denied.success is False
    assert "Recovery selection expired" in denied.content
    assert session.guards == [] and session.granted == []
    assert slot.resume_authorized is False


def test_session_without_guard_setter_is_refused() -> None:
    session = _Session()
    del _Session.set_dispatch_guard
    try:
        denied, _ = _authorize(_Host(), session, dispatch_guard=lambda: True)
    finally:
        _Session.set_dispatch_guard = lambda self, guard: self.guards.append(guard)  # type: ignore[method-assign]
    assert denied is not None and denied.success is False


def test_explicit_resume_is_granted_and_recorded() -> None:
    session = _Session()
    denied, slot = _authorize(_Host("danso"), session, resume_task=True)
    assert denied is None
    assert session.granted == ["resume"]
    assert slot.resume_authorized is True
    # An explicit resume never also authorizes an interactive follow-up.
    assert slot.followup_authorized is False


def test_explicit_resume_without_runtime_support_is_a_typed_refusal() -> None:
    session = _Session()
    del _Session.authorize_task_resume
    try:
        denied, slot = _authorize(_Host("danso"), session, resume_task=True)
    finally:
        _Session.authorize_task_resume = lambda self: self.granted.append("resume")  # type: ignore[method-assign]
    assert denied is not None
    assert denied.error == "danso_task_resume_unavailable"
    assert denied.session_id == "sess-1"
    assert slot.resume_authorized is False


@pytest.mark.parametrize(
    ("provider", "usage_mode", "expected"),
    [
        ("danso", MODE_INTERACTIVE, True),
        ("danso", "cron", False),
        ("claude", MODE_INTERACTIVE, False),
    ],
)
def test_followup_is_granted_only_for_interactive_danso_turns(
    provider: str, usage_mode: str, expected: bool
) -> None:
    session = _Session()
    denied, slot = _authorize(_Host(provider), session, usage_mode=usage_mode)
    assert denied is None
    assert slot.followup_authorized is expected
    assert session.granted == (["followup"] if expected else [])


def test_followup_is_skipped_when_the_runtime_lacks_it() -> None:
    session = _Session()
    del _Session.authorize_task_followup
    try:
        denied, slot = _authorize(_Host("danso"), session)
    finally:
        _Session.authorize_task_followup = lambda self: self.granted.append("followup")  # type: ignore[method-assign]
    assert denied is None
    assert slot.followup_authorized is False


def test_failure_after_the_resume_grant_leaves_it_visible_to_the_caller() -> None:
    # The slot is the whole reason this is not a return-tuple helper: the
    # caller's finally must still see (and clear) a resume authorization that
    # was granted before a later step raised.
    class _Explodes(_Session):
        def authorize_task_followup(self) -> None:
            raise RuntimeError("runtime went away")

    session = _Explodes()
    slot = _TurnAuthorization()
    # resume_task=False + interactive danso => follow-up path runs after a
    # resume grant would have; force the resume grant first by calling with
    # resume_task=True on a slot, then simulate the follow-up failure.
    denied, slot = _authorize(_Host("danso"), session, resume_task=True, authorization=slot)
    assert denied is None and slot.resume_authorized is True
    with pytest.raises(RuntimeError):
        _authorize(_Host("danso"), session, authorization=slot)
    assert slot.resume_authorized is True
    assert slot.followup_authorized is False
