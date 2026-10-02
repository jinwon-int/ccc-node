"""Messages the Matrix frontend refuses must not vanish without a word (#2002).

``Policy.admit`` only answers "run it or not"; an oversize text, an edit or a
thread reply used to disappear silently. ``Policy.rejection`` names the
refusals the sender can act on and the transport answers each once.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

import pytest

from telegram_bot.core.matrix.state import (
    MAX_TEXT_BYTES,
    REJECT_EDIT,
    REJECT_TEXT_TOO_LARGE,
    REJECT_THREAD,
)
from telegram_bot.core.matrix.transport import (
    NOTICE_EDIT_IGNORED,
    NOTICE_THREAD_IGNORED,
    NOTICE_UNSUPPORTED_KIND,
)
from test_matrix_state import BOT, GROUP, NOW, ROOM, event, policy
from test_matrix_transport import fake_nio, running

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _with(content: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    raw = event()
    raw["content"] = content
    raw.update(overrides)
    return raw


OVERSIZE = "가" * (MAX_TEXT_BYTES // 3 + 10)  # 3 bytes per Hangul syllable


# --- Policy.rejection -------------------------------------------------------------


def test_an_oversize_direct_message_is_refused_with_a_reason() -> None:
    raw = _with({"msgtype": "m.text", "body": OVERSIZE})
    assert policy().admit(ROOM, raw, decrypted=True, now_ms=NOW) is None
    assert policy().rejection(ROOM, raw, decrypted=True, now_ms=NOW) == REJECT_TEXT_TOO_LARGE


def test_a_normal_message_has_no_rejection() -> None:
    assert policy().rejection(ROOM, event(), decrypted=True, now_ms=NOW) is None


def test_an_edit_and_a_thread_reply_are_named() -> None:
    edit = _with(
        {
            "msgtype": "m.text",
            "body": "* fixed",
            "m.new_content": {"msgtype": "m.text", "body": "fixed"},
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$orig"},
        }
    )
    thread = _with(
        {"msgtype": "m.text", "body": "in a thread", "m.relates_to": {"rel_type": "m.thread", "event_id": "$root"}}
    )
    assert policy().rejection(ROOM, edit, decrypted=True, now_ms=NOW) == REJECT_EDIT
    assert policy().rejection(ROOM, thread, decrypted=True, now_ms=NOW) == REJECT_THREAD


def test_family_rooms_only_answer_refusals_that_address_the_bot() -> None:
    unaddressed = _with({"msgtype": "m.text", "body": OVERSIZE})
    assert policy().rejection(GROUP, unaddressed, decrypted=True, now_ms=NOW) is None
    addressed = _with({"msgtype": "m.text", "body": OVERSIZE, "m.mentions": {"user_ids": [BOT]}})
    assert policy().rejection(GROUP, addressed, decrypted=True, now_ms=NOW) == REJECT_TEXT_TOO_LARGE
    # An edit is gated on the *new* content's address.
    edit = _with(
        {
            "msgtype": "m.text",
            "body": "* x",
            "m.new_content": {"msgtype": "m.text", "body": "x", "m.mentions": {"user_ids": [BOT]}},
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$orig"},
        }
    )
    assert policy().rejection(GROUP, edit, decrypted=True, now_ms=NOW) == REJECT_EDIT


def test_the_bots_own_status_notice_and_its_edit_are_neither_run_nor_answered() -> None:
    """The external-wait status line is an m.notice with m.replace edits (#2088)."""
    status = {"msgtype": "m.notice", "body": "⏳ Waiting for results · PR #598 CI"}
    edit = {"msgtype": "m.notice", "body": "* ✅ CI green",
            "m.new_content": {"msgtype": "m.notice", "body": "✅ CI green"},
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$status"}}
    for content in (status, edit):
        for room in (ROOM, GROUP):
            raw = _with(content, sender=BOT)
            assert policy().admit(room, raw, decrypted=True, now_ms=NOW) is None
            assert policy().rejection(room, raw, decrypted=True, now_ms=NOW) is None


def test_ineligible_events_stay_silent() -> None:
    oversize = {"msgtype": "m.text", "body": OVERSIZE}
    assert policy().rejection(ROOM, _with(oversize, sender=BOT), decrypted=True, now_ms=NOW) is None
    assert policy().rejection(ROOM, _with(oversize, origin_server_ts=NOW - 90_000_000), decrypted=True, now_ms=NOW) is None
    assert policy().rejection(ROOM, _with(oversize), decrypted=False, now_ms=NOW) is None
    assert policy().rejection("!unknown:example.test", _with(oversize), decrypted=True, now_ms=NOW) is None


# --- transport notices --------------------------------------------------------------


def _install_nio() -> Any:
    nio = fake_nio()

    class StickerEvent:
        def __init__(self, sender: str, event_id: str, sender_key: str, ts: int) -> None:
            self.sender = sender
            self.event_id = event_id
            self.sender_key = sender_key
            self.server_timestamp = ts
            self.decrypted = True
            self.verified = True
            self.source: dict[str, Any] = {}

    class RoomMessageEmote(StickerEvent):
        pass

    class RoomMessageNotice(StickerEvent):
        pass

    nio.StickerEvent = StickerEvent  # type: ignore[attr-defined]
    nio.RoomMessageEmote = RoomMessageEmote  # type: ignore[attr-defined]
    nio.RoomMessageNotice = RoomMessageNotice  # type: ignore[attr-defined]
    sys.modules["nio"] = nio
    return nio


async def test_the_transport_answers_each_refusal_once(tmp_path: Path) -> None:
    async with running(tmp_path) as h:
        f = h.f
        nio = _install_nio()
        owner, room = f.c["owner"], f.c["rooms"][0]
        f.trusted[owner] = {"DEV": "b" * 43}
        f.c["not_before_ms"] = now = int(time.time() * 1000) - 1000

        def message(event_id: str, content: dict[str, Any]) -> Any:
            source = {"type": "m.room.message", "event_id": event_id, "sender": owner,
                      "origin_server_ts": now + 500, "content": content}
            return nio.RoomMessageText(sender=owner, source=source, verified=True, sender_key="b" * 43, ts=now + 500)

        big = message("$big", {"msgtype": "m.text", "body": OVERSIZE})
        assert f.admit_event(room, big) is None
        assert f.admit_event(room, big) is None  # a replayed sync batch: still one notice
        edit = {"msgtype": "m.text", "body": "* fixed", "m.new_content": {"msgtype": "m.text", "body": "fixed"},
                "m.relates_to": {"rel_type": "m.replace", "event_id": "$orig"}}
        assert f.admit_event(room, message("$edit1", edit)) is None
        assert f.admit_event(room, message("$edit2", edit)) is None  # second edit of the same message
        thread = {"msgtype": "m.text", "body": "t", "m.relates_to": {"rel_type": "m.thread", "event_id": "$root"}}
        assert f.admit_event(room, message("$thread", thread)) is None
        assert f.admit_event(room, message("$ok", {"msgtype": "m.text", "body": "hi"})) is not None

        replies = h.replies()
        too_large = [r for r in replies if "너무 길어" in r]
        assert len(too_large) == 1
        assert f"한도 {MAX_TEXT_BYTES // 1024} KiB" in too_large[0]
        assert replies.count(NOTICE_EDIT_IGNORED) == 1
        assert replies.count(NOTICE_THREAD_IGNORED) == 1
        counts = f.store.get_meta("ignored_messages")
        # Idempotent like the notices: a replay or a second edit is not recounted.
        assert counts[REJECT_TEXT_TOO_LARGE] == 1 and counts[REJECT_EDIT] == 1 and counts[REJECT_THREAD] == 1
        assert OVERSIZE not in str(counts), "the counter is body-free"


async def test_stickers_get_one_notice_per_direct_room_and_untrusted_ones_none(tmp_path: Path) -> None:
    async with running(tmp_path) as h:
        f = h.f
        nio = _install_nio()
        owner, room = f.c["owner"], f.c["rooms"][0]
        f.trusted[owner] = {"DEV": "b" * 43}
        f.c["not_before_ms"] = now = int(time.time() * 1000) - 1000
        assert f.admit_event(room, nio.StickerEvent(owner, "$s1", "b" * 43, now + 500)) is None
        assert f.admit_event(room, nio.StickerEvent(owner, "$s2", "b" * 43, now + 600)) is None
        assert f.admit_event(room, nio.StickerEvent(owner, "$s3", "z" * 43, now + 700)) is None  # untrusted key
        assert f.admit_event(room, nio.StickerEvent(owner, "$s1", "b" * 43, now + 500)) is None  # replay
        assert f.admit_event(room, nio.RoomMessageEmote(owner, "$e1", "b" * 43, now + 800)) is None
        assert f.admit_event(room, nio.RoomMessageNotice(owner, "$n1", "b" * 43, now + 900)) is None
        assert f.admit_event(room, nio.StickerEvent(owner, "$old", "b" * 43, now - 90_000_000)) is None  # stale
        assert h.replies().count(NOTICE_UNSUPPORTED_KIND) == 1
        counts = f.store.get_meta("ignored_messages")
        assert counts["StickerEvent"] == 2 and counts["RoomMessageEmote"] == 1 and counts["RoomMessageNotice"] == 1


async def test_the_bots_own_notices_echoed_by_sync_are_not_counted_or_answered(tmp_path: Path) -> None:
    """Our m.notice status line and its edits come back through sync (#2088)."""
    async with running(tmp_path) as h:
        f = h.f
        nio = _install_nio()
        account, room = f.c["account"], f.c["rooms"][0]
        f.c["not_before_ms"] = now = int(time.time() * 1000) - 1000
        status = nio.RoomMessageNotice(account, "$status", "", now + 500)
        status.source = {"content": {"msgtype": "m.notice", "body": "⏳ Waiting for results"}}
        edit = nio.RoomMessageNotice(account, "$status-edit", "", now + 600)
        edit.source = {"content": {"msgtype": "m.notice", "body": "* ✅ CI green",
                                   "m.new_content": {"msgtype": "m.notice", "body": "✅ CI green"},
                                   "m.relates_to": {"rel_type": "m.replace", "event_id": "$status"}}}
        assert f.admit_event(room, status) is None
        assert f.admit_event(room, edit) is None
        assert h.replies() == []
        assert f.store.get_meta("ignored_messages") is None
        assert f.store.get_meta("unsupported_kind_counted") is None


async def test_rewording_a_notice_in_a_later_release_never_stops_the_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Notice rows are permanent; a same-key/different-text notice raises SafetyStop (review of #2009)."""
    import telegram_bot.core.matrix.transport as transport_module

    async with running(tmp_path) as h:
        f = h.f
        nio = _install_nio()
        owner, room = f.c["owner"], f.c["rooms"][0]
        f.trusted[owner] = {"DEV": "b" * 43}
        f.c["not_before_ms"] = now = int(time.time() * 1000) - 1000

        def edit(event_id: str, target: str) -> Any:
            content = {"msgtype": "m.text", "body": "* x", "m.new_content": {"msgtype": "m.text", "body": "x"},
                       "m.relates_to": {"rel_type": "m.replace", "event_id": target}}
            source = {"type": "m.room.message", "event_id": event_id, "sender": owner,
                      "origin_server_ts": now + 500, "content": content}
            return nio.RoomMessageText(sender=owner, source=source, verified=True, sender_key="b" * 43, ts=now + 500)

        f.admit_event(room, edit("$e1", "$orig"))
        monkeypatch.setattr(transport_module, "NOTICE_EDIT_IGNORED", "✏️ reworded in a later release")
        f.admit_event(room, edit("$e2", "$orig"))  # must not raise notice-identity-conflict
        assert "✏️ reworded in a later release" in h.replies()
        # A malformed edit target falls back to the edit event itself.
        f.admit_event(room, edit("$e3", "$" + "x" * 5000))
        assert h.replies().count("✏️ reworded in a later release") == 2
