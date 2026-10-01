"""End-to-end contract for the #2075 legacy group-session migration tool.

``scripts/ccc_session_scope_audit.py`` edits ``sessions.json`` outside the
bridge, so these tests pin it against the real ``SessionStore`` and the real
first-use seed: the tool must flag exactly the row the seed contaminates, the
store must still load after ``--apply``, and the cleared room row must neither
be re-seeded from the DM row nor resume anything on the next turn.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from telegram_bot.session.manager import SessionManager
from telegram_bot.session.store import SessionStore

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ccc_session_scope_audit.py"
OWNER = 934719283
ROOM = -1001234567890
ROOM_KEY = f"{OWNER}:{ROOM}"
DM_SID = "0b6f3c1e-5d2a-4c1b-9f10-2a7e8d9c4b21"


@pytest.fixture
def audit_tool(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sys, "path", list(sys.path))
    before = set(sys.modules)
    spec = importlib.util.spec_from_file_location("ccc_session_scope_audit_e2e", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    yield module
    for name in set(sys.modules) - before:
        if name.startswith(("ccc_", "_ccc_")):
            sys.modules.pop(name, None)


def _manager(path: Path) -> SessionManager:
    manager = SessionManager(
        SessionStore(path),
        SimpleNamespace(agent_provider="claude", auto_new_session_after_hours=None),
    )
    manager.initialize()
    return manager


def _bot(manager: SessionManager) -> Any:
    chat_logger = sys.modules.get("telegram_bot.utils.chat_logger")
    if chat_logger is not None and not callable(getattr(chat_logger, "log_debug", None)):
        sys.modules.pop("telegram_bot.utils.chat_logger", None)
        sys.modules.pop("telegram_bot.core.bot", None)
    from telegram_bot.core.bot import TelegramBot

    bot = TelegramBot.__new__(TelegramBot)
    bot._session_manager = manager
    bot._config = SimpleNamespace(
        agent_provider="claude", telegram_session_scope="per-user-chat"
    )
    bot._runtime_active_sessions = set()
    return bot


async def _seed_room(manager: SessionManager) -> dict:
    bot = _bot(manager)
    room = await manager.get_session(ROOM_KEY)
    await bot._seed_scoped_session_from_legacy(ROOM_KEY, OWNER, ROOM, room)
    return room


def test_tool_flags_the_row_the_legacy_seed_contaminates(tmp_path: Path, audit_tool) -> None:
    store = tmp_path / "sessions.json"

    async def exercise() -> None:
        manager = _manager(store)
        await manager.patch_session(OWNER, updates={"session_id": DM_SID, "model": "opus"})
        room = await _seed_room(manager)
        assert room["session_id"] == DM_SID  # the pre-#2075 shape

    asyncio.run(exercise())
    audit = audit_tool.audit_store(store)
    assert [(row.key, row.reason) for row in audit.flagged] == [(ROOM_KEY, "dm-session")]


def test_applied_store_loads_and_the_room_starts_fresh(tmp_path: Path, audit_tool) -> None:
    store = tmp_path / "sessions.json"

    async def contaminate() -> None:
        manager = _manager(store)
        await manager.patch_session(OWNER, updates={"session_id": DM_SID, "model": "opus"})
        await _seed_room(manager)

    asyncio.run(contaminate())
    backup = audit_tool.apply_audit(audit_tool.audit_store(store))
    assert backup is not None and backup.exists()

    async def after_apply() -> None:
        manager = _manager(store)  # validates every entry on load
        dm = await manager.get_session(OWNER)
        assert dm["session_id"] == DM_SID and dm["model"] == "opus"
        room = await manager.get_session(ROOM_KEY)
        assert room["session_id"] is None and room["new_session"] is True
        assert room["model"] == "opus"  # preferences survive

        # The next room turn re-runs the first-use seed: it must not copy the
        # DM id back, and nothing is resumable for the room.
        bot = _bot(manager)
        seeded = await _seed_room(manager)
        assert seeded["session_id"] is None
        assert (await manager.get_session(ROOM_KEY))["session_id"] is None
        assert bot._effective_session_id(ROOM_KEY, seeded) is None

    asyncio.run(after_apply())
    assert audit_tool.audit_store(store).flagged == []
