"""「한데」 external-wait status as ``kind:"status"`` notices (#2590 A).

Config knobs, the transport's ``send_status_notice`` frame, and the
``HandeWaitStatusMixin`` re-send rules (turn_closed, text change, interval,
same-room active turn, nothing monitored) against fakes.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from telegram_bot.core.external_wait import STATE_MONITORING
from telegram_bot.core.hande.state import (
    DEFAULT_WAIT_STATUS_INTERVAL_S,
    HandeConfigError,
    validate_config,
)
from telegram_bot.core.hande.transport import HandeTransport
from telegram_bot.core.hande.wait_status import HandeWaitStatusMixin
from telegram_bot.core.matrix.wait_status import MatrixWaitStatusMixin

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _config(**overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "account": "@bridgenode:hande",
        "owner": "@owner:hande",
        "rooms": ["agent", "fleet"],
        "family_rooms": ["fleet"],
        "family_users": ["@kid:hande"],
        "socket_path": "/tmp/unused-hande.sock",
    }
    config.update(overrides)
    return config


# -- config ------------------------------------------------------------------------


def test_wait_status_knobs_default_off_and_bounded() -> None:
    knobs = validate_config(_config())
    assert knobs["wait_status_frames"] is False
    assert knobs["wait_status_interval_s"] == DEFAULT_WAIT_STATUS_INTERVAL_S == 120
    assert validate_config(_config(wait_status_frames=True, wait_status_interval_s=30))[
        "wait_status_interval_s"
    ] == 30
    with pytest.raises(HandeConfigError, match="wait_status_frames"):
        validate_config(_config(wait_status_frames="yes"))
    for bad in (29, 241, 120.0, True, "120"):
        with pytest.raises(HandeConfigError, match="wait_status_interval_s"):
            validate_config(_config(wait_status_interval_s=bad))


# -- transport -----------------------------------------------------------------------


class _Client:
    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []
        self.closed = False
        self.connected_at = self.last_frame_at = time.monotonic()

    async def send(self, frame: dict[str, Any]) -> bool:
        self.frames.append(dict(frame))
        return True


class _Runner:
    async def run(self, *a: Any, **k: Any) -> Any:  # pragma: no cover - never driven here
        raise AssertionError


def _transport(**overrides: Any) -> tuple[HandeTransport, _Client]:
    transport = HandeTransport(_config(**overrides), _Runner())  # type: ignore[arg-type]
    client = _Client()
    transport._client = client  # type: ignore[assignment]
    return transport, client


async def test_send_status_notice_needs_both_knobs() -> None:
    for knobs in ({}, {"wait_status_frames": True}, {"notice_frames": True}):
        transport, client = _transport(**knobs)
        assert transport.wait_status_ready is False
        assert await transport.send_status_notice("agent", "⏳ Waiting") is False
        assert client.frames == []
        assert transport.health_signals(time.time())["wait_status_sent"] == 0


async def test_send_status_notice_frame_shape_and_guards() -> None:
    transport, client = _transport(wait_status_frames=True, notice_frames=True)
    assert transport.wait_status_ready is True
    assert await transport.send_status_notice("agent", "⏳ Waiting for results · PR #1 CI") is True
    assert client.frames == [
        {"type": "notice", "room": "agent", "text": "⏳ Waiting for results · PR #1 CI", "kind": "status"}
    ]
    assert await transport.send_status_notice("other", "⏳ x") is False, "room outside the config"
    assert await transport.send_status_notice("agent", "   ") is False
    client.closed = True
    assert await transport.send_status_notice("agent", "⏳ x") is False, "never held for a reconnecting bot"
    signals = transport.health_signals(time.time())
    assert signals["wait_status_frames"] is True and signals["wait_status_sent"] == 1


def test_active_room() -> None:
    transport, _ = _transport()
    assert transport.active_room() is None

    class _Turn:
        job = {"room_id": "fleet"}

    transport._active = _Turn()  # type: ignore[assignment]
    assert transport.active_room() == "fleet"


# -- projection ----------------------------------------------------------------------


class _FakeTransport:
    def __init__(self) -> None:
        self.wait_status_ready = True
        self.wait_status_interval_s = 120.0
        self.active: str | None = None
        self.sent: list[tuple[str, str]] = []

    def active_room(self) -> str | None:
        return self.active

    async def send_status_notice(self, room: str, text: str) -> bool:
        self.sent.append((room, text))
        return True


class _Registry:
    def __init__(self) -> None:
        self.recs: list[dict[str, Any]] = []

    def records(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.recs]


def _wait(pr: int, *, state: str = STATE_MONITORING, user: int = 7, chat: int = 70) -> dict[str, Any]:
    now = time.time()
    return {
        "wait_id": f"w-{pr}",
        "user_id": user,
        "chat_id": chat,
        "state": state,
        "source": "github_pr_checks",
        "pr_number": pr,
        "summary": "",
        "created_epoch": now - 60,
        "expires_epoch": now + 3600,
    }


class _Host(HandeWaitStatusMixin, MatrixWaitStatusMixin):
    def __init__(self, *, wait_status_frames: bool = True, notice_frames: bool = True) -> None:
        self._transport = _FakeTransport()
        self.registry = _Registry()
        self.config = {"wait_status_frames": wait_status_frames, "notice_frames": notice_frames}

    def room_for_chat(self, chat_id: int) -> str | None:
        return {70: "agent", 71: "fleet"}.get(chat_id)

    def _external_wait_registry(self) -> Any:
        return self.registry

    def load_config(self) -> dict[str, Any]:
        return self.config

    def _control_identity(self, job: Any) -> tuple[int, int]:
        return int(job["user"]), int(job["chat"])


async def test_turn_closed_sends_status_while_monitored_then_interval_and_change() -> None:
    host = _Host()
    host.registry.recs = [_wait(603)]
    await host._wait_status_turn_closed({"user": 7, "chat": 70})
    assert len(host._transport.sent) == 1
    room, text = host._transport.sent[0]
    assert room == "agent" and text.startswith("⏳ Waiting for results · PR #603 CI")
    # Same text inside the interval: no re-send from the loop.
    await host._hande_wait_status_tick()
    assert len(host._transport.sent) == 1
    # Interval elapsed: re-sent so the apps' 5-minute expiry never hits.
    key = "7:70"
    sent_at, text_hash = host._hande_wait_state()[key]
    host._hande_wait_state()[key] = (sent_at - 121, text_hash)
    await host._hande_wait_status_tick()
    assert len(host._transport.sent) == 2
    # Text change (a second wait registered): sent at once.
    host.registry.recs.append(_wait(604))
    await host._hande_wait_status_tick()
    assert len(host._transport.sent) == 3 and "PR #604" in host._transport.sent[-1][1]
    # turn_closed always re-sends (the reply cleared the line on the apps).
    await host._wait_status_turn_closed({"user": 7, "chat": 70})
    assert len(host._transport.sent) == 4


async def test_nothing_monitored_sends_nothing_and_forgets_route() -> None:
    host = _Host()
    host.registry.recs = [_wait(603)]
    await host._wait_status_turn_closed({"user": 7, "chat": 70})
    assert len(host._transport.sent) == 1
    host.registry.recs = [dict(_wait(603), state="success", terminal_status="success", completed_epoch=time.time())]
    await host._wait_status_turn_closed({"user": 7, "chat": 70})
    await host._hande_wait_status_tick()
    assert len(host._transport.sent) == 1, "a finished wait is announced by the resume turn, not a status line"
    assert host._hande_wait_state() == {}


async def test_same_room_active_turn_owns_the_slot() -> None:
    host = _Host()
    host.registry.recs = [_wait(603), _wait(700, chat=71)]
    host._transport.active = "agent"
    await host._hande_wait_status_tick()
    assert [room for room, _ in host._transport.sent] == ["fleet"], "only the idle room gets its line"


async def test_knobs_off_or_transport_not_ready_change_nothing() -> None:
    host = _Host(wait_status_frames=False)
    host.registry.recs = [_wait(603)]
    # The re-send loop returns at once when the config knobs are off.
    await asyncio.wait_for(host._reconcile_external_wait_status_on_start(), 1)
    host._transport.wait_status_ready = False
    await host._wait_status_turn_closed({"user": 7, "chat": 70})
    await host._hande_wait_status_tick()
    await host._sync_external_wait_status(7, 70)
    assert host._transport.sent == []
    # No outbox rows on hande: the delivery hook is a no-op.
    await host._wait_status_delivered({"event_id": "$notice-x"})


async def test_loop_runs_ticks_until_cancelled() -> None:
    host = _Host()
    host.registry.recs = [_wait(603)]
    task = asyncio.ensure_future(host._reconcile_external_wait_status_on_start())
    for _ in range(50):
        if host._transport.sent:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(host._transport.sent) == 1


async def test_route_without_room_is_skipped() -> None:
    host = _Host()
    host.registry.recs = [_wait(603, chat=99)]
    await host._hande_wait_status_tick()
    assert host._transport.sent == []
