"""Matrix /memory_promote (#2004).

``create_app`` never handed the ``memory_promoter`` to ``MatrixBot`` and the
command was not parsed, so ``/memory_promote`` went to the agent as plain text.
These tests pin the port of Telegram's contract: owner-only (#1955),
audience-scoped mode with a promoter wired, the owner's private DM only (the
Matrix-routed private scope), one exact fact id, and the same answers.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from telegram_bot.core.matrix.bot import OWNER_ONLY_COMMAND, MatrixBot
from telegram_bot.core.memory_audience import resolve_memory_audience
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

FACT = "distill-" + "b" * 12
DEST = "promoted-" + "a" * 16


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def matrix_config(_shared: dict[str, Any]) -> dict[str, Any]:  # noqa: F811 - re-exposed under its own name
    return _shared


def _scoped_bot(tmp_path: Path, *, promoted: bool = True, **overrides: Any) -> tuple[MatrixBot, Any]:
    settings = dict(
        bridge_memory_mode="audience-scoped",
        telegram_session_scope="shared-groups",
        bridge_memory_audience_root=tmp_path / "audiences",
        bridge_memory_audience_key_path=tmp_path / "audience.key",
    )
    settings.update(overrides)
    bot, chat, _manager = _bot(tmp_path, **settings)
    bot._memory_promoter = Mock(
        promote=Mock(return_value=SimpleNamespace(promoted=promoted, destination_fact_id=DEST))
    )
    bot._distill_local_sink_worker = SimpleNamespace(refresh_route=AsyncMock())
    return bot, chat


async def _say(bot: MatrixBot, body: str, *, sender: str = OWNER, room: str = DM_ROOM, kind: str = "direct") -> str:
    result = await bot.run_turn(_job(body, sender=sender, room=room), sink=FakeSink(), session_id=None, room_kind=kind)
    return result.text


async def test_owner_dm_promotes_from_the_matrix_private_scope(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, chat = _scoped_bot(tmp_path)

    text = await _say(bot, f"/memory_promote {FACT}")

    assert text == f"✅ Promoted {FACT} to shared memory as {DEST}."
    owner = bot.ids.user_id(OWNER)
    expected = resolve_memory_audience(bot._settings, user_id=owner, chat_id=owner, route="matrix")
    telegram = resolve_memory_audience(bot._settings, user_id=owner, chat_id=owner, route="telegram")
    assert expected is not None and telegram is not None
    bot._memory_promoter.promote.assert_called_once_with(source_scope=expected.scope, fact_id=FACT)
    assert expected.scope != telegram.scope  # this frontend's own private route
    bot._distill_local_sink_worker.refresh_route.assert_awaited_once_with(audience="shared", scope="shared")
    assert chat.calls == []  # a command, never an agent turn


async def test_already_promoted_fact_reports_refresh(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, _chat = _scoped_bot(tmp_path, promoted=False)
    assert await _say(bot, f"/memory_promote {FACT}") == (
        f"✅ {FACT} was already promoted as {DEST}; shared memory was refreshed."
    )


async def test_memory_promote_is_owner_only(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, chat = _scoped_bot(tmp_path, execution_profile="strict-project")
    text = await _say(bot, f"/memory_promote {FACT}", sender=KID, room=FAMILY_ROOM, kind="family")
    assert text == OWNER_ONLY_COMMAND
    bot._memory_promoter.promote.assert_not_called()
    bot._distill_local_sink_worker.refresh_route.assert_not_awaited()
    assert chat.calls == []


async def test_missing_promoter_or_mode_off_is_a_graceful_answer(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    unavailable = "ℹ️ Explicit memory promotion is unavailable on this bridge."
    # Default construction (what create_app did before #2004): no promoter.
    bare, chat, _manager = _bot(tmp_path, bridge_memory_mode="audience-scoped")
    assert await _say(bare, f"/memory_promote {FACT}") == unavailable
    assert chat.calls == []

    off, _chat = _scoped_bot(tmp_path / "off", bridge_memory_mode="off")
    assert await _say(off, f"/memory_promote {FACT}") == unavailable
    off._memory_promoter.promote.assert_not_called()

    no_sink, _chat = _scoped_bot(tmp_path / "nosink")
    no_sink._distill_local_sink_worker = None
    assert await _say(no_sink, f"/memory_promote {FACT}") == unavailable
    no_sink._memory_promoter.promote.assert_not_called()


async def test_family_room_and_bad_arguments_are_refused(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, _chat = _scoped_bot(tmp_path)
    assert await _say(bot, f"/memory_promote {FACT}", room=FAMILY_ROOM, kind="family") == (
        "❌ Memory promotion is allowed only from your private DM."
    )
    usage = "Usage: /memory_promote distill-<12 lowercase hex>"
    for body in ("/memory_promote", "/memory_promote arbitrary", f"/memory_promote {FACT.upper()}", f"/memory_promote {FACT} extra"):
        assert await _say(bot, body) == usage
    bot._memory_promoter.promote.assert_not_called()
    bot._distill_local_sink_worker.refresh_route.assert_not_awaited()


@pytest.mark.parametrize(
    ("error", "answer"),
    [
        (LookupError("missing"), "ℹ️ That fact was not found in your private memory."),
        (ValueError("secret-looking body"), "⚠️ That private fact is not eligible for promotion."),
        (RuntimeError("secret-looking body"), "⚠️ Memory promotion could not be completed. You can retry safely."),
    ],
)
async def test_promoter_failures_map_to_body_free_answers(
    tmp_path: Path, matrix_config: dict[str, Any], caplog: pytest.LogCaptureFixture, error: Exception, answer: str
) -> None:
    bot, _chat = _scoped_bot(tmp_path)
    bot._memory_promoter.promote.side_effect = error
    assert await _say(bot, f"/memory_promote {FACT}") == answer
    bot._distill_local_sink_worker.refresh_route.assert_not_awaited()
    assert "secret-looking body" not in caplog.text
