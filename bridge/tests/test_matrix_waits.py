"""Matrix /waits and /cancelwait (#2004).

The Matrix frontend has watched external waits since #1934, but a room had no
way to list or cancel them. These tests pin the commands: owner-only (#1955),
scoped to the requester's own waits (legacy records without ``user_id``
included, as on Telegram), and a wait registered by someone else is never
cancelled.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from telegram_bot.core.external_wait import (
    STATE_MONITORING,
    TERMINAL_OWNER_CANCEL,
    ExternalWaitRegistry,
    default_registry_path,
    render_waits,
)
from telegram_bot.core.matrix.bot import OWNER_ONLY_COMMAND, MatrixBot
from test_matrix_bot import (
    DM_ROOM,
    FAMILY_ROOM,
    KID,
    OWNER,
    FakeSink,
    _bot,
    _job,
)
from test_matrix_bot import matrix_config as _shared  # noqa: F401 - fixture registration below

pytestmark = pytest.mark.anyio

SHA_A = "a" * 40
SHA_B = "b" * 40


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def matrix_config(_shared: dict[str, Any]) -> dict[str, Any]:  # noqa: F811 - re-exposed under its own name
    return _shared


def _registry(bot: MatrixBot) -> ExternalWaitRegistry:
    return ExternalWaitRegistry(default_registry_path(bot._data_dir() / "external-wait"))


def _register(bot: MatrixBot, *, user: str, sha: str, summary: str) -> str:
    user_id = bot.ids.user_id(user)
    return _registry(bot).register(
        repo="jinwon-int/ccc-node",
        pr_number=1998,
        head_sha=sha,
        user_id=user_id,
        chat_id=user_id,
        session_id="s-1",
        summary=summary,
        timeout_seconds=3600,
        poll_interval_seconds=30,
    )


async def _say(bot: MatrixBot, body: str, *, sender: str = OWNER, room: str = DM_ROOM, kind: str = "direct") -> str:
    result = await bot.run_turn(_job(body, sender=sender, room=room), sink=FakeSink(), session_id=None, room_kind=kind)
    return result.text


async def test_waits_lists_only_the_requesters_waits(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, chat, _manager = _bot(tmp_path)
    assert await _say(bot, "/waits") == "No external waits registered."
    mine = _register(bot, user=OWNER, sha=SHA_A, summary="merge after CI")
    theirs = _register(bot, user=KID, sha=SHA_B, summary="kid wait")
    text = await _say(bot, "/waits")
    assert text.startswith("External waits (GitHub CI):")
    assert f"`{mine}`" in text and "merge after CI" in text
    assert theirs not in text and "kid wait" not in text
    assert chat.calls == []  # a command, never an agent turn


async def test_cancelwait_cancels_only_the_requesters_wait(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, _chat, _manager = _bot(tmp_path)
    mine = _register(bot, user=OWNER, sha=SHA_A, summary="mine")
    theirs = _register(bot, user=KID, sha=SHA_B, summary="theirs")

    assert await _say(bot, "/cancelwait") == "Usage: /cancelwait <wait_id> — see /waits"
    assert await _say(bot, f"/cancelwait {theirs}") == f"No active external wait with id `{theirs}`."
    assert _registry(bot).get(theirs)["state"] == STATE_MONITORING
    assert await _say(bot, "/cancelwait nope") == "No active external wait with id `nope`."

    assert await _say(bot, f"/cancelwait {mine}") == f"Cancelled external wait `{mine}`."
    assert _registry(bot).get(mine)["state"] == TERMINAL_OWNER_CANCEL
    # Already terminal: idempotent "not active".
    assert await _say(bot, f"/cancelwait {mine}") == f"No active external wait with id `{mine}`."


async def test_waits_commands_are_owner_only(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, chat, _manager = _bot(tmp_path, execution_profile="strict-project")
    theirs = _register(bot, user=KID, sha=SHA_B, summary="kid wait")
    for body in ("/waits", f"/cancelwait {theirs}"):
        assert await _say(bot, body, sender=KID, room=FAMILY_ROOM, kind="family") == OWNER_ONLY_COMMAND
    assert _registry(bot).get(theirs)["state"] == STATE_MONITORING
    assert chat.calls == []


async def test_cancelwait_refreshes_the_waits_status_message(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    """Owner cancellation has no wake: the command itself edits the status (#2088)."""
    from telegram_bot.core.matrix.bot import MatrixTurnRunner
    from test_matrix_wait_status import StatusTransport

    bot, _chat, _manager = _bot(tmp_path)
    transport = StatusTransport()
    bot._transport = transport
    bot._job_identity(_job("hi"), "direct")
    owner = bot.ids.user_id(OWNER)
    mine = _register(bot, user=OWNER, sha=SHA_A, summary="mine")
    await bot._sync_external_wait_status(owner, owner)
    event = transport.deliver("$notice-1")
    await MatrixTurnRunner(bot).delivered({"event_id": "$notice-1"})

    assert await _say(bot, f"/cancelwait {mine}") == f"Cancelled external wait `{mine}`."
    assert transport.edits[-1][:2] == (DM_ROOM, event)
    assert transport.edits[-1][2].startswith("🚫 cancelled by owner · PR #1998 CI")
    # A refused cancel touches nothing.
    assert await _say(bot, f"/cancelwait {mine}") == f"No active external wait with id `{mine}`."
    assert len(transport.edits) == 1


def test_telegram_and_matrix_share_one_renderer() -> None:
    from telegram_bot.core.bot_commands import BotCommandMixin

    records = [
        {"wait_id": "w1", "repo": "o/r", "pr_number": 7, "head_sha": SHA_A, "state": STATE_MONITORING, "summary": "s"},
        {"wait_id": "w2", "repo": "o/r", "pr_number": 8, "head_sha": SHA_B, "state": "success"},
    ]
    assert BotCommandMixin._render_waits(records) == render_waits(records)
    assert render_waits(records).splitlines() == [
        "External waits (GitHub CI):",
        f"⏳ `w1` — o/r#7 @ {SHA_A[:8]} · monitoring",
        "   ↳ s",
        f"🏁 `w2` — o/r#8 @ {SHA_B[:8]} · success",
    ]
