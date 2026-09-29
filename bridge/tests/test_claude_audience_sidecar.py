"""Per-turn Claude session_id -> memory audience sidecar (#1921).

The audience-scoped nunchi collector can only route a Claude transcript through
this record, so every successful Claude turn must leave exactly one body-free,
owner-only sidecar under the resolved audience — and nothing else.
"""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from telegram_bot.core.claude_audience_sidecar import (
    SIDECAR_SCHEMA,
    claude_session_map_dir,
    record_claude_turn_audience,
)
from telegram_bot.core.memory_audience import MemoryAudience
from telegram_bot.core.project_chat_types import ChatResponse
from telegram_bot.session.manager import SessionManager
from telegram_bot.session.store import SessionStore

OWNER = 934719283
FAMILY_ROOM = -1001234567890
SID = "0b6f3c1e-5d2a-4c1b-9f10-2a7e8d9c4b21"
SECRET_BODY = "the owner's private reply body"
SECRET_REQUEST = "the owner's private question"


def _manager(tmp_path: Path, provider: str) -> SessionManager:
    manager = SessionManager(
        SessionStore(tmp_path / "sessions.json"),
        SimpleNamespace(agent_provider=provider, auto_new_session_after_hours=None),
    )
    manager.initialize()
    return manager


def _telegram_bot(tmp_path: Path, *, provider: str = "claude", mode: str = "audience-scoped") -> Any:
    chat_logger = sys.modules.get("telegram_bot.utils.chat_logger")
    if chat_logger is not None and not callable(getattr(chat_logger, "log_debug", None)):
        sys.modules.pop("telegram_bot.utils.chat_logger", None)
        sys.modules.pop("telegram_bot.core.bot", None)
    from telegram_bot.core.bot import TelegramBot

    bot = TelegramBot.__new__(TelegramBot)
    bot._session_manager = _manager(tmp_path, provider)
    bot._config = SimpleNamespace(
        agent_provider=provider,
        claude_settings_path=Path("/path/that/must/not/be/read"),
        bridge_memory_mode=mode,
        telegram_session_scope="per-user-chat",
        bridge_memory_audience_root=tmp_path / "audiences",
        bridge_memory_audience_key_path=tmp_path / "keys" / "audience.key",
    )
    bot._project_chat = SimpleNamespace()
    bot._runtime_active_sessions = set()
    bot._clock = SimpleNamespace(time=lambda: 1000.0)
    bot._distill_journal = None
    return bot


def _sidecars(root: Path) -> list[Path]:
    return sorted(root.glob("*/claude/session-map/*.json"))


@pytest.mark.anyio
async def test_owner_dm_turn_writes_private_sidecar(tmp_path: Path) -> None:
    bot = _telegram_bot(tmp_path)

    await bot._save_session_id(
        OWNER,
        ChatResponse(SECRET_BODY, session_id=SID),
        user_id=OWNER,
        chat_id=OWNER,
        request_text=SECRET_REQUEST,
    )

    session = await bot._session_manager.get_session(OWNER)
    files = _sidecars(tmp_path / "audiences")
    assert [p.name for p in files] == [f"{SID}.json"]
    record = json.loads(files[0].read_text())
    assert record["schema"] == SIDECAR_SCHEMA
    assert record["provider"] == "claude"
    assert record["session_id"] == SID
    # Same resolution the session store (and so distill) already uses.
    assert record["memory_audience"] == "private" == session["distill_memory_audience"]
    assert record["memory_scope"] == session["distill_memory_scope"]
    assert record["memory_scope"].startswith("private-")
    assert files[0].parent.parent.parent.name == record["memory_scope"]


@pytest.mark.anyio
async def test_family_room_turn_writes_shared_sidecar(tmp_path: Path) -> None:
    bot = _telegram_bot(tmp_path)

    await bot._save_session_id(
        f"{OWNER}:{FAMILY_ROOM}",
        ChatResponse(SECRET_BODY, session_id=SID),
        user_id=OWNER,
        chat_id=FAMILY_ROOM,
    )

    files = _sidecars(tmp_path / "audiences")
    assert [str(p.relative_to(tmp_path / "audiences")) for p in files] == [
        f"shared/claude/session-map/{SID}.json"
    ]
    record = json.loads(files[0].read_text())
    assert (record["memory_audience"], record["memory_scope"]) == ("shared", "shared")


@pytest.mark.anyio
async def test_sidecar_is_body_free_owner_only_and_rewritten_per_turn(tmp_path: Path) -> None:
    bot = _telegram_bot(tmp_path)
    for _ in range(2):
        await bot._save_session_id(
            OWNER,
            ChatResponse(SECRET_BODY, session_id=SID),
            user_id=OWNER,
            chat_id=OWNER,
            request_text=SECRET_REQUEST,
        )

    files = _sidecars(tmp_path / "audiences")
    assert len(files) == 1  # idempotent: one record per session, no temp debris
    assert not [p for p in files[0].parent.iterdir() if p.name.startswith(".")]
    raw = files[0].read_text()
    record = json.loads(raw)
    assert set(record) == {
        "schema", "provider", "session_id", "memory_audience", "memory_scope", "updated_at",
    }
    for leaked in (SECRET_BODY, SECRET_REQUEST, str(OWNER)):
        assert leaked not in raw
    assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
    assert stat.S_IMODE(files[0].parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(files[0].parent.parent.stat().st_mode) == 0o700


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("provider", "mode"), [("codex", "audience-scoped"), ("piri", "audience-scoped"), ("claude", "off")]
)
async def test_no_sidecar_outside_claude_audience_scoped(
    tmp_path: Path, provider: str, mode: str
) -> None:
    bot = _telegram_bot(tmp_path, provider=provider, mode=mode)

    await bot._save_session_id(
        OWNER, ChatResponse("ok", session_id=SID), user_id=OWNER, chat_id=OWNER
    )

    assert _sidecars(tmp_path / "audiences") == []


@pytest.mark.anyio
async def test_failed_turn_writes_no_sidecar(tmp_path: Path) -> None:
    bot = _telegram_bot(tmp_path)

    await bot._save_session_id(
        OWNER,
        ChatResponse("failed", success=False, session_id=SID, error="boom"),
        user_id=OWNER,
        chat_id=OWNER,
    )

    assert _sidecars(tmp_path / "audiences") == []


@pytest.mark.anyio
async def test_unsafe_session_id_is_refused_without_failing_the_turn(tmp_path: Path) -> None:
    bot = _telegram_bot(tmp_path)

    await bot._save_session_id(
        OWNER, ChatResponse("ok", session_id="../escape"), user_id=OWNER, chat_id=OWNER
    )

    session = await bot._session_manager.get_session(OWNER)
    assert session["session_id"] == "../escape"  # the turn itself is unaffected
    assert _sidecars(tmp_path / "audiences") == []
    assert not list((tmp_path / "audiences").rglob("*escape*"))


@pytest.mark.anyio
async def test_symlinked_map_dir_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "audiences"
    audience = MemoryAudience("shared", "shared", root)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (root / "shared" / "claude").mkdir(parents=True)
    claude_session_map_dir(audience).symlink_to(elsewhere)

    assert await record_claude_turn_audience("claude", audience, SID) is False
    assert list(elsewhere.iterdir()) == []


@pytest.mark.anyio
async def test_matrix_turn_writes_sidecar_for_its_route(tmp_path: Path) -> None:
    from telegram_bot.core.matrix.bot import MatrixBot

    bot = MatrixBot.__new__(MatrixBot)
    bot._settings = SimpleNamespace(
        agent_provider="claude",
        bridge_memory_mode="audience-scoped",
        telegram_session_scope="per-user-chat",
        bridge_memory_audience_root=tmp_path / "audiences",
        bridge_memory_audience_key_path=tmp_path / "keys" / "audience.key",
    )
    bot._project_chat = SimpleNamespace(_memory_route="matrix")
    bot._session_manager = SimpleNamespace(patch_session=AsyncMock())
    bot._runtime_active_sessions = set()

    await bot._save_session_id(
        "k", ChatResponse(SECRET_BODY, session_id=SID), user_id=OWNER, chat_id=OWNER
    )

    updates = bot._session_manager.patch_session.await_args.kwargs["updates"]
    files = _sidecars(tmp_path / "audiences")
    assert len(files) == 1
    record = json.loads(files[0].read_text())
    assert record["memory_scope"] == updates["distill_memory_scope"]
    assert SECRET_BODY not in files[0].read_text()


def _collector():
    import importlib.util

    path = Path(__file__).resolve().parents[2] / "claude/hooks/nunchi/claude-audience-feed.py"
    spec = importlib.util.spec_from_file_location("claude_audience_feed", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.anyio
async def test_collector_contract_routes_bridge_written_sidecars(tmp_path: Path) -> None:
    """Writer and reader agree: DM -> its private scope, room -> shared."""

    bot = _telegram_bot(tmp_path)
    room_sid = "1c2d3e4f-0000-4000-8000-00000000000a"
    await bot._save_session_id(
        OWNER, ChatResponse("ok", session_id=SID), user_id=OWNER, chat_id=OWNER
    )
    await bot._save_session_id(
        f"{OWNER}:{FAMILY_ROOM}",
        ChatResponse("ok", session_id=room_sid),
        user_id=OWNER,
        chat_id=FAMILY_ROOM,
    )
    root = tmp_path / "audiences"
    root.chmod(0o700)
    session = await bot._session_manager.get_session(OWNER)

    index = _collector().SidecarIndex(root)

    assert index.records == 2
    assert index.resolve(SID) == ("routed", ("private", session["distill_memory_scope"]))
    assert index.resolve(room_sid) == ("routed", ("shared", "shared"))
    assert index.resolve("99999999-0000-4000-8000-000000000000") == ("unmapped", None)
