"""Matrix external-wait status message (#2088, stage 2 of #2081).

Mirrors the Telegram sync tests in ``test_external_wait_status.py`` against
``MatrixBot`` with a status-capable ``FakeTransport``: the first message is a
durable outbox notice (its event id is adopted once the sender delivers it),
later changes are ``m.replace`` edits of that event and the clean-up is a
redaction. Renderer, planner and store are the shared stage-1 ones.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from telegram_bot.core.external_wait import (
    TERMINAL_FAILURE,
    TERMINAL_SUCCESS,
    ExternalWaitRegistry,
    default_registry_path,
)
from telegram_bot.core.external_wait_status import (
    ExternalWaitStatusStore,
    default_status_store_path,
    render_wait_status,
    text_hash_of,
)
from telegram_bot.core.matrix import wait_status
from telegram_bot.core.matrix.bot import MatrixBot, MatrixTurnRunner
from telegram_bot.core.matrix.wait_status import PENDING_PREFIX
from test_matrix_bot import (
    DM_ROOM,
    FAMILY_ROOM,
    FakeSink,
    FakeTransport,
    _bot as _matrix_bot,
    _job,
)
from test_matrix_bot import matrix_config as _shared  # noqa: F401 - fixture registration below

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def matrix_config(_shared: dict[str, Any]) -> dict[str, Any]:  # noqa: F811 - re-exposed under its own name
    return _shared


class StatusTransport(FakeTransport):
    """FakeTransport plus the outbox-row / edit / redact seam the projection needs."""

    def __init__(self) -> None:
        super().__init__(None, None)
        self.rows: dict[str, dict[str, Any]] = {}
        self.edits: list[tuple[str, str, str]] = []
        self.redactions: list[tuple[str, str]] = []
        self.edit_result: Any = True
        self.redact_result: Any = True
        self._seq = 0

    def enqueue_notice(self, room_id: str, text: str, *, key: str | None = None) -> str:  # type: ignore[override]
        super().enqueue_notice(room_id, text, key=key)
        self._seq += 1
        row = f"$notice-{self._seq}"
        self.rows[row] = {"room": room_id, "text": text, "state": "pending", "event": None}
        return row

    def deliver(self, row: str) -> str:
        event = f"$event-{row.rsplit('-', 1)[1]}"
        self.rows[row].update(state="sent", event=event)
        return event

    def notice_event(self, row_id: str) -> tuple[str, str | None]:
        row = self.rows.get(row_id)
        if row is None:
            return "gone", None
        return row["state"], row["event"]

    async def edit_notice(self, room: str, event_id: str, text: str) -> bool:
        self.edits.append((room, event_id, text))
        if isinstance(self.edit_result, BaseException):
            raise self.edit_result
        return bool(self.edit_result)

    async def redact_notice(self, room: str, event_id: str) -> bool:
        self.redactions.append((room, event_id))
        if isinstance(self.redact_result, BaseException):
            raise self.redact_result
        return bool(self.redact_result)


def _setup(tmp_path: Path) -> tuple[MatrixBot, StatusTransport, ExternalWaitRegistry, ExternalWaitStatusStore, int, int]:
    bot, _chat, _manager = _matrix_bot(tmp_path)
    transport = StatusTransport()
    bot._transport = transport
    user_id, chat_id, _room = bot._job_identity(_job("hi"), "direct")  # learn the DM route
    home = bot._data_dir() / "external-wait"
    registry = ExternalWaitRegistry(default_registry_path(home))
    store = ExternalWaitStatusStore(default_status_store_path(home))
    return bot, transport, registry, store, user_id, chat_id


def _register(registry: ExternalWaitRegistry, user_id: int, chat_id: int, pr: int = 598) -> str:
    return registry.register(
        repo="jinwon-int/ccc-node",
        pr_number=pr,
        head_sha="abc1234",
        user_id=user_id,
        chat_id=chat_id,
        session_id="sess-1",
        summary="squash-merge the PR",
        timeout_seconds=6 * 3600,
        poll_interval_seconds=30,
        now=time.time(),
    )


async def _deliver(bot: MatrixBot, transport: StatusTransport, row: str) -> str:
    """What the sender does: deliver the row, then fire the runner's hook."""
    event = transport.deliver(row)
    await MatrixTurnRunner(bot).delivered({"event_id": row, "room_id": DM_ROOM})
    return event


# --- the outbox -> edit -> redact lifecycle --------------------------------------


async def test_send_is_queued_then_adopted_edited_and_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, matrix_config: dict[str, Any]
) -> None:
    bot, transport, registry, store, uid, cid = _setup(tmp_path)
    # No waits, no stored message: silence.
    await bot._sync_external_wait_status(uid, cid)
    assert transport.notices == []

    wait_id = _register(registry, uid, cid)
    await bot._sync_external_wait_status(uid, cid)
    assert len(transport.notices) == 1
    room, text = transport.notices[0]
    assert room == DM_ROOM and text.startswith("⏳ Waiting for results · PR #598 CI")
    assert transport.notice_keys[0] is not None and transport.notice_keys[0].startswith("wait-status-")
    stored = store.get(uid, cid)
    assert stored is not None and stored["message_id"] == PENDING_PREFIX + "$notice-1"
    assert stored["text_hash"] == text_hash_of(text)

    # Terminal transition while the row is still queued: nothing to edit yet.
    registry.finish(wait_id, TERMINAL_SUCCESS)
    await bot._sync_external_wait_status(uid, cid)
    assert transport.edits == [] and len(transport.notices) == 1

    # The sender delivers it: the event id is adopted and the pending change lands.
    event = await _deliver(bot, transport, "$notice-1")
    assert len(transport.edits) == 1
    assert transport.edits[0][:2] == (DM_ROOM, event)
    assert transport.edits[0][2].startswith("✅ CI green → continuing · PR #598 CI")
    assert store.get(uid, cid)["message_id"] == event
    assert store.get(uid, cid)["text_hash"] == text_hash_of(transport.edits[0][2])

    # Same state again: no Matrix call at all.
    await bot._sync_external_wait_status(uid, cid)
    assert len(transport.edits) == 1 and len(transport.notices) == 1

    # Thirty-one minutes later nothing recent is left: redact and forget.
    later = time.time() + 31 * 60
    monkeypatch.setattr(wait_status, "time", SimpleNamespace(time=lambda: later, time_ns=time.time_ns))
    await bot._sync_external_wait_status(uid, cid)
    assert transport.redactions == [(DM_ROOM, event)]
    assert store.get(uid, cid) is None
    await bot._sync_external_wait_status(uid, cid)
    assert len(transport.notices) == 1 and len(transport.redactions) == 1


async def test_delivered_without_a_change_only_adopts_the_event_id(
    tmp_path: Path, matrix_config: dict[str, Any]
) -> None:
    bot, transport, registry, store, uid, cid = _setup(tmp_path)
    _register(registry, uid, cid)
    await bot._sync_external_wait_status(uid, cid)
    event = await _deliver(bot, transport, "$notice-1")
    assert store.get(uid, cid)["message_id"] == event
    assert transport.edits == [] and transport.redactions == []


async def test_delivery_hook_ignores_rows_it_does_not_track(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, transport, registry, store, uid, cid = _setup(tmp_path)
    _register(registry, uid, cid)
    await bot._sync_external_wait_status(uid, cid)
    runner = MatrixTurnRunner(bot)
    await runner.delivered({"event_id": "$e1"})  # a turn's reply row
    await runner.delivered({"event_id": "$notice-unrelated"})
    assert store.get(uid, cid)["message_id"] == PENDING_PREFIX + "$notice-1"


async def test_a_row_that_produced_no_event_is_forgotten_and_resent(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, transport, registry, store, uid, cid = _setup(tmp_path)
    _register(registry, uid, cid)
    store.put(uid, cid, message_id=PENDING_PREFIX + "$notice-lost", text_hash="h")
    await bot._sync_external_wait_status(uid, cid)
    assert len(transport.notices) == 1
    assert store.get(uid, cid)["message_id"] == PENDING_PREFIX + "$notice-1"


async def test_edit_refused_resends_while_monitoring(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, transport, registry, store, uid, cid = _setup(tmp_path)
    _register(registry, uid, cid)
    store.put(uid, cid, message_id="$old", text_hash="stale-hash")
    transport.edit_result = False
    await bot._sync_external_wait_status(uid, cid)
    assert [e[1] for e in transport.edits] == ["$old"] and len(transport.notices) == 1
    assert store.get(uid, cid)["message_id"] == PENDING_PREFIX + "$notice-1"


async def test_edit_refused_without_monitoring_just_forgets(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, transport, registry, store, uid, cid = _setup(tmp_path)
    wait_id = _register(registry, uid, cid)
    registry.finish(wait_id, TERMINAL_FAILURE)
    store.put(uid, cid, message_id="$old", text_hash="stale-hash")
    transport.edit_result = False
    await bot._sync_external_wait_status(uid, cid)
    assert len(transport.edits) == 1 and transport.notices == []
    assert store.get(uid, cid) is None


async def test_transient_edit_failures_keep_the_stored_hash(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, transport, registry, store, uid, cid = _setup(tmp_path)
    _register(registry, uid, cid)
    store.put(uid, cid, message_id="$old", text_hash="stale-hash")
    transport.edit_result = ConnectionError("room-muted")
    await bot._sync_external_wait_status(uid, cid)
    assert store.get(uid, cid)["text_hash"] == "stale-hash" and transport.notices == []
    transport.edit_result = True
    await bot._sync_external_wait_status(uid, cid)  # retried on the next pass
    assert len(transport.edits) == 2
    assert store.get(uid, cid)["text_hash"] == text_hash_of(transport.edits[-1][2])


async def test_redaction_failures(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, transport, registry, store, uid, cid = _setup(tmp_path)
    store.put(uid, cid, message_id="$old", text_hash="h")
    transport.redact_result = ConnectionError("network down")
    await bot._sync_external_wait_status(uid, cid)
    assert len(transport.redactions) == 1 and store.get(uid, cid) is not None  # retried next sync
    transport.redact_result = False  # already gone / not redactable
    await bot._sync_external_wait_status(uid, cid)
    assert len(transport.redactions) == 2 and store.get(uid, cid) is None


async def test_unknown_room_never_sends_and_drops_a_stale_entry(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, transport, registry, store, uid, cid = _setup(tmp_path)
    _register(registry, uid, 999_999)
    await bot._sync_external_wait_status(uid, 999_999)
    assert transport.notices == []
    store.put(uid, 999_998, message_id="$old", text_hash="h")
    await bot._sync_external_wait_status(uid, 999_998)
    assert transport.redactions == [] and store.get(uid, 999_998) is None


async def test_transport_without_the_status_seam_stays_silent(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, _transport, registry, store, uid, cid = _setup(tmp_path)
    plain = FakeTransport(None, None)  # enqueue_notice only: could never edit/redact
    bot._transport = plain
    _register(registry, uid, cid)
    await bot._sync_external_wait_status(uid, cid)
    assert plain.notices == [] and store.get(uid, cid) is None
    bot._transport = None
    await bot._sync_external_wait_status(uid, cid)


async def test_a_row_id_less_enqueue_stores_nothing(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, transport, registry, store, uid, cid = _setup(tmp_path)
    transport.enqueue_notice = lambda room, text, key=None: None  # type: ignore[method-assign,assignment]
    _register(registry, uid, cid)
    await bot._sync_external_wait_status(uid, cid)
    assert store.get(uid, cid) is None


async def test_sync_is_gated_by_env_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, matrix_config: dict[str, Any]) -> None:
    bot, transport, registry, store, uid, cid = _setup(tmp_path)
    _register(registry, uid, cid)
    monkeypatch.setenv("CCC_EXTERNAL_WAIT_STATUS", "false")
    assert bot._external_wait_status_syncer() is None
    monitor = bot._build_external_wait_monitor()
    assert monitor is not None and monitor._status_syncer is None
    await bot._sync_external_wait_status(uid, cid)
    await bot._reconcile_external_wait_status_on_start()
    await MatrixTurnRunner(bot).turn_closed(_job("hi"))
    await MatrixTurnRunner(bot).delivered({"event_id": "$notice-1"})
    assert transport.notices == []
    monkeypatch.delenv("CCC_EXTERNAL_WAIT_STATUS")
    assert bot._external_wait_status_syncer() is not None
    monitor = bot._build_external_wait_monitor()
    assert monitor is not None and monitor._status_syncer is not None


async def test_sync_swallows_registry_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, matrix_config: dict[str, Any]) -> None:
    bot, transport, registry, store, uid, cid = _setup(tmp_path)

    def boom(self: Any) -> Any:
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(MatrixBot, "_external_wait_registry", boom)
    await bot._sync_external_wait_status(uid, cid)
    await bot._reconcile_external_wait_status_on_start()
    assert transport.notices == [] and transport.edits == [] and transport.redactions == []


async def test_concurrent_syncs_post_one_message(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, transport, registry, store, uid, cid = _setup(tmp_path)
    _register(registry, uid, cid)
    await asyncio.gather(*(bot._sync_external_wait_status(uid, cid) for _ in range(5)))
    assert len(transport.notices) == 1


# --- triggers ---------------------------------------------------------------------


async def test_turn_closed_hook_posts_the_status_for_the_turns_route(
    tmp_path: Path, matrix_config: dict[str, Any]
) -> None:
    bot, transport, registry, store, uid, cid = _setup(tmp_path)
    chat = bot._project_chat

    async def registers_a_wait(kwargs: dict[str, Any]) -> None:
        _register(registry, kwargs["user_id"], kwargs["chat_id"])

    chat.on_process = registers_a_wait
    job = _job("merge it once CI is green")
    runner = MatrixTurnRunner(bot)
    result = await runner.run(job, sink=FakeSink(), session_id=None, room_kind="direct")
    assert result.text == "answer"
    assert transport.notices == []  # nothing during the turn itself
    await runner.turn_closed(job)
    assert len(transport.notices) == 1
    assert transport.notices[0][0] == DM_ROOM
    assert transport.notices[0][1].startswith("⏳ Waiting for results · PR #598 CI")
    # A turn in a route without waits stays silent.
    await runner.turn_closed(_job("other", room=FAMILY_ROOM))
    assert len(transport.notices) == 1


async def test_turn_closed_with_an_unmappable_job_is_ignored(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, transport, registry, store, uid, cid = _setup(tmp_path)
    await MatrixTurnRunner(bot).turn_closed({"event_id": "$x", "room_id": DM_ROOM, "sender": "not-a-user"})
    assert transport.notices == []


async def test_monitor_terminal_transition_edits_the_delivered_status(
    tmp_path: Path, matrix_config: dict[str, Any]
) -> None:
    from telegram_bot.core.external_wait_monitor import PrState

    bot, transport, registry, store, uid, cid = _setup(tmp_path)
    wait_id = _register(registry, uid, cid)
    await bot._sync_external_wait_status(uid, cid)
    event = await _deliver(bot, transport, "$notice-1")
    monitor = bot._build_external_wait_monitor()
    assert monitor is not None

    class _Gh:
        async def fetch_pr_state(self, repo: str, pr_number: int) -> PrState:
            return PrState(head_sha="abc1234", rollup="failure")

    monitor._transport = _Gh()
    monitor._resumer = None
    await monitor._tick()
    assert registry.get(wait_id)["terminal_status"] == TERMINAL_FAILURE
    # The wake notice went through the outbox; the status was edited in place.
    assert len(transport.notices) == 2 and "598" in transport.notices[1][1]
    assert transport.edits and transport.edits[-1][1] == event
    assert transport.edits[-1][2].startswith("❌ CI failed → investigating · PR #598 CI")


# --- restart --------------------------------------------------------------------------


async def test_startup_reconcile_adopts_refreshes_finalizes_and_drops(
    tmp_path: Path, matrix_config: dict[str, Any]
) -> None:
    bot, transport, registry, store, uid, cid = _setup(tmp_path)
    kid_uid, family_cid, _room = bot._job_identity(_job("hi", sender="@kid:example.org", room=FAMILY_ROOM), "family")
    # Route A (owner DM): monitoring wait whose message was never sent.
    _register(registry, uid, cid, pr=1)
    # Route B (family room): stored, delivered while down, wait finished -> final line.
    done = _register(registry, kid_uid, family_cid, pr=2)
    registry.finish(done, TERMINAL_FAILURE)
    transport.rows["$notice-old"] = {"room": FAMILY_ROOM, "text": "x", "state": "sent", "event": "$family-event"}
    store.put(kid_uid, family_cid, message_id=PENDING_PREFIX + "$notice-old", text_hash="old")
    # Route C: stored, nothing left (pruned/stale) -> redact. Same room as A's
    # user but another conversation int, mapped to the DM for the test.
    store.put(uid, 424242, message_id="$stale", text_hash="old")
    bot.ids._reverse[424242] = DM_ROOM  # type: ignore[attr-defined]
    # Malformed store entry is skipped.
    raw = json.loads(store.path.read_text(encoding="utf-8"))
    raw["junk"] = {"message_id": 1, "user_id": "x"}
    store.path.write_text(json.dumps(raw), encoding="utf-8")

    await bot._reconcile_external_wait_status_on_start()

    assert [room for room, _text in transport.notices] == [DM_ROOM]
    assert store.get(uid, cid)["message_id"].startswith(PENDING_PREFIX)
    assert [(room, event) for room, event, _text in transport.edits] == [(FAMILY_ROOM, "$family-event")]
    assert transport.edits[0][2].startswith("❌ CI failed → investigating · PR #2 CI")
    assert store.get(kid_uid, family_cid)["message_id"] == "$family-event"
    assert transport.redactions == [(DM_ROOM, "$stale")] and store.get(uid, 424242) is None


async def test_startup_reconcile_is_bounded_and_quiet_when_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, matrix_config: dict[str, Any]
) -> None:
    bot, transport, registry, store, uid, cid = _setup(tmp_path)
    await bot._reconcile_external_wait_status_on_start()
    assert transport.notices == [] and transport.redactions == []
    monkeypatch.setattr(wait_status, "MAX_RECONCILE_ROUTES", 2)
    for n in range(3):
        store.put(uid, cid + n, message_id=f"$m{n}", text_hash="old")
        bot.ids._reverse[cid + n] = DM_ROOM  # type: ignore[attr-defined]
    await bot._reconcile_external_wait_status_on_start()
    assert len(transport.redactions) == 2


async def test_current_entry_is_left_alone(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, transport, registry, store, uid, cid = _setup(tmp_path)
    _register(registry, uid, cid)
    current = render_wait_status(registry.records_for_route(uid, cid), time.time())
    assert current is not None
    store.put(uid, cid, message_id="$live", text_hash=text_hash_of(current))
    await bot._reconcile_external_wait_status_on_start()
    assert transport.notices == [] and transport.edits == [] and transport.redactions == []
