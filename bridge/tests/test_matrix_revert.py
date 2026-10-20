"""Matrix ``/revert`` (#2004): Telegram's two-step revert as a numbered text menu.

Telegram picks the message and then the mode with inline buttons; Matrix has
none, so step 1 numbers the recent user messages (same transcript source and
limit), step 2 numbers the same modes, and the owner answers with a bare
number (or the one-shot ``/revert <n> [<mode>]``). The transcript rewrite is
the shared ``revert.revert_conversation``. Pending menus are per (room,
sender), owner-only (#1955), expire, and never swallow an ordinary message.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from telegram_bot.core import revert as revert_ops
from telegram_bot.core.matrix import bot as matrix_bot
from telegram_bot.core.matrix.bot import (
    OWNER_ONLY_COMMAND,
    REVERT_MENU_EXPIRED,
    SUPPORTED_COMMANDS,
    MatrixBot,
)
from telegram_bot.core.project_chat_history import ProjectChatHistoryMixin
from test_matrix_bot import (
    DM_ROOM,
    FAMILY_ROOM,
    KID,
    OWNER,
    FakeProjectChat,
    FakeSessionManager,
    FakeSink,
    _job,
    _settings,
)
from test_matrix_bot import matrix_config as _shared  # noqa: F401 - fixture registration below

pytestmark = pytest.mark.anyio

NOW = 1_700_000_000.0
SESSION = "s-0"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def matrix_config(_shared: dict[str, Any]) -> dict[str, Any]:  # noqa: F811 - re-exposed under its own name
    return _shared


class RevertProjectChat(ProjectChatHistoryMixin, FakeProjectChat):
    """The real transcript reader (``get_conversation_history``) over a tmp dir."""

    def __init__(self, conversations_dir: Path) -> None:
        super().__init__()
        self.conversations_dir = conversations_dir
        self.cleared: list[tuple[int, int | None]] = []

    async def clear_user_stream(self, user_id: int, chat_id: int | None = None) -> None:
        self.cleared.append((user_id, chat_id))


def _transcript(directory: Path, users: list[str], session_id: str = SESSION) -> Path:
    """User/assistant pairs: user message ``n`` sits at JSONL line ``2 * n``."""

    directory.mkdir(parents=True, exist_ok=True)
    lines = []
    for n, text in enumerate(users):
        stamp = f"2023-11-14T22:{n:02d}:00Z"
        lines.append({"type": "user", "timestamp": stamp, "message": {"role": "user", "content": text}})
        lines.append({"type": "assistant", "timestamp": stamp, "message": {"role": "assistant", "content": [{"type": "text", "text": f"re: {text}"}]}})
    path = directory / f"{session_id}.jsonl"
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    return path


class _Clock:
    def __init__(self) -> None:
        self.now = NOW

    def time(self) -> float:
        return self.now


def _revert_bot(
    tmp_path: Path, users: list[str] | None = None, **overrides: Any
) -> tuple[MatrixBot, RevertProjectChat, FakeSessionManager, Path, _Clock]:
    overrides.setdefault("agent_provider", "claude")
    settings = _settings(tmp_path, **overrides)
    chat = RevertProjectChat(tmp_path / "conversations")
    path = _transcript(chat.conversations_dir, users if users is not None else ["first", "second", "third"])
    manager = FakeSessionManager(provider=settings.agent_provider)
    clock = _Clock()
    bot = MatrixBot(settings, project_chat=chat, session_manager=manager, clock=clock)
    owner = bot.ids.user_id(OWNER)
    manager.rows[owner] = {"provider": settings.agent_provider, "session_id": SESSION}
    return bot, chat, manager, path, clock


async def _say(bot: MatrixBot, body: str, *, sender: str = OWNER, room: str = DM_ROOM, kind: str = "direct") -> str:
    result = await bot.run_turn(_job(body, sender=sender, room=room), sink=FakeSink(), session_id=None, room_kind=kind)
    return result.text


def _lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Path, str, int]]:
    calls: list[tuple[Path, str, int]] = []
    real = revert_ops.revert_conversation

    def recording(conversations_dir: Path, session_id: str, msg_index: int) -> bool:
        calls.append((Path(conversations_dir), session_id, msg_index))
        return real(conversations_dir, session_id, msg_index)

    monkeypatch.setattr(matrix_bot.revert_ops, "revert_conversation", recording)
    return calls


def test_revert_is_a_supported_owner_only_command() -> None:
    assert "revert" in SUPPORTED_COMMANDS
    assert "revert" in matrix_bot._OWNER_ONLY_COMMANDS
    assert MatrixBot._parse_command("/revert 2 conv") == ("revert", ["2", "conv"])


async def test_list_select_mode_reverts_through_the_shared_executor(
    tmp_path: Path, matrix_config: dict[str, Any], spy: list[tuple[Path, str, int]]
) -> None:
    bot, chat, _manager, path, _clock = _revert_bot(tmp_path)
    owner = bot.ids.user_id(OWNER)

    listing = await _say(bot, "/revert")
    assert listing.startswith("🔄 Select a message to revert to:")
    # Newest first, as Telegram's keyboard.
    assert listing.index("1. 💬") < listing.index("third") < listing.index("2. 💬") < listing.index("second")
    assert "3. 💬" in listing and "first" in listing
    assert "Reply with a number" in listing

    modes = await _say(bot, "2")
    assert modes.startswith("🔄 Selected message:\n\nsecond...")
    for number, (_mode, label) in enumerate(revert_ops.REVERT_MODES, 1):
        assert f"{number}. {label}" in modes
    assert spy == []

    done = await _say(bot, "2")  # 2 = conv
    assert done.startswith("✅ Reverted to before:\n\n[2023-11-14 22:01:00]\nsecond")
    assert spy == [(chat.conversations_dir, SESSION, 2)]
    # The JSONL keeps exactly the lines before the chosen user message.
    assert [line["message"]["content"] for line in _lines(path)] == [
        "first",
        [{"type": "text", "text": "re: first"}],
    ]
    assert chat.cleared == [(owner, owner)]
    assert (owner, owner) in chat.stop_calls  # active work cancelled first
    assert bot._revert_choices == {}
    assert chat.calls == []  # no number reached the agent
    # The menu is closed: the next "1" is an ordinary turn.
    await _say(bot, "1")
    assert [call["user_message"] for call in chat.calls] == ["1"]


async def test_one_shot_forms(
    tmp_path: Path, matrix_config: dict[str, Any], spy: list[tuple[Path, str, int]]
) -> None:
    bot, chat, _manager, path, _clock = _revert_bot(tmp_path)

    # /revert <n> <mode> with no menu open: newest-first numbering, mode by name.
    assert (await _say(bot, "/revert 3 conv")).startswith("✅ Reverted to before:")
    assert spy == [(chat.conversations_dir, SESSION, 0)]
    assert path.read_text(encoding="utf-8") == ""

    bot2, chat2, _m2, path2, _c2 = _revert_bot(tmp_path / "two")
    # /revert <n> opens the mode step; the mode may also be given by number.
    assert (await _say(bot2, "/revert 1")).startswith("🔄 Selected message:\n\nthird")
    assert (await _say(bot2, "/revert 1 1")).startswith("✅ Reverted to before:")
    assert spy[-1] == (chat2.conversations_dir, SESSION, 4)
    assert len(_lines(path2)) == 4
    assert chat.calls == [] and chat2.calls == []


async def test_non_owner_is_refused_and_menus_are_per_sender(
    tmp_path: Path, matrix_config: dict[str, Any], spy: list[tuple[Path, str, int]]
) -> None:
    bot, chat, manager, path, _clock = _revert_bot(tmp_path, execution_profile="strict-project")
    before = path.read_text(encoding="utf-8")
    for body in ("/revert", "/revert 1 conv", "/revert cancel"):
        assert await _say(bot, body, sender=KID, room=FAMILY_ROOM, kind="family") == OWNER_ONLY_COMMAND
    assert bot._revert_choices == {}

    # The owner opens a menu in the family room; the kid's "1" there is the
    # kid's own ordinary turn, never a pick on the owner's menu.
    family = bot.ids.chat_id(FAMILY_ROOM, OWNER, direct=False)
    manager.rows[bot._conversation_key(bot.ids.user_id(OWNER), family)] = {
        "provider": "claude", "session_id": SESSION,
    }
    assert (await _say(bot, "/revert", room=FAMILY_ROOM, kind="family")).startswith("🔄 Select")
    await _say(bot, "1", sender=KID, room=FAMILY_ROOM, kind="family")
    assert [call["user_message"] for call in chat.calls] == ["1"]
    assert (bot.ids.user_id(OWNER), family) in bot._revert_choices
    # Nor does the owner's DM answer the owner's family-room menu.
    await _say(bot, "1")
    assert [call["user_message"] for call in chat.calls] == ["1", "1"]
    assert spy == [] and path.read_text(encoding="utf-8") == before


async def test_menu_expires(
    tmp_path: Path, matrix_config: dict[str, Any], spy: list[tuple[Path, str, int]]
) -> None:
    bot, chat, _manager, _path, clock = _revert_bot(tmp_path)

    await _say(bot, "/revert")
    clock.now += matrix_bot._REVERT_CHOICE_TTL_S - 1
    assert (await _say(bot, "1")).startswith("🔄 Selected message:")  # still open; refreshes the TTL
    clock.now += matrix_bot._REVERT_CHOICE_TTL_S
    assert await _say(bot, "2") == REVERT_MENU_EXPIRED
    assert bot._revert_choices == {}
    assert chat.calls == [] and spy == []
    # After the expiry answer a number is an ordinary turn again.
    await _say(bot, "2")
    assert [call["user_message"] for call in chat.calls] == ["2"]


async def test_invalid_numbers_and_arguments(
    tmp_path: Path, matrix_config: dict[str, Any], spy: list[tuple[Path, str, int]]
) -> None:
    bot, chat, _manager, _path, _clock = _revert_bot(tmp_path)
    usage = MatrixBot._REVERT_USAGE

    assert await _say(bot, "/revert 9") == "❌ Invalid number, please try again."
    for body in ("/revert x", "/revert 1 bogus", "/revert 1 2 3", "/revert page", "/revert page 0", "/revert 1 6"):
        assert await _say(bot, body) == usage, body

    await _say(bot, "/revert")
    for wrong in ("0", "4", "99"):
        assert await _say(bot, wrong) == "❌ Invalid number, please try again."
    assert (await _say(bot, "1")).startswith("🔄 Selected message:")  # menu stayed open
    for wrong in ("0", "6"):
        assert await _say(bot, wrong) == "❌ Invalid number, please try again."
    assert bot._revert_choices  # still waiting for a mode
    assert chat.calls == [] and spy == []


async def test_ordinary_message_while_pending_runs_as_a_turn(
    tmp_path: Path, matrix_config: dict[str, Any], spy: list[tuple[Path, str, int]]
) -> None:
    bot, chat, _manager, _path, _clock = _revert_bot(tmp_path)

    await _say(bot, "/revert")
    assert await _say(bot, "hello there") == "answer"
    assert [call["user_message"] for call in chat.calls] == ["hello there"]
    assert bot._revert_choices == {}  # the menu was abandoned, not kept
    await _say(bot, "1")
    assert [call["user_message"] for call in chat.calls] == ["hello there", "1"]

    # Mid mode step too, and "1st" is not a selection.
    await _say(bot, "/revert")
    await _say(bot, "1")
    await _say(bot, "1st")
    assert chat.calls[-1]["user_message"] == "1st"
    assert bot._revert_choices == {}

    # Another command abandons the menu as well.
    await _say(bot, "/revert")
    await _say(bot, "/stop")
    assert bot._revert_choices == {}
    assert spy == []


async def test_cancel_paths(
    tmp_path: Path, matrix_config: dict[str, Any], spy: list[tuple[Path, str, int]]
) -> None:
    bot, chat, _manager, path, _clock = _revert_bot(tmp_path)
    before = path.read_text(encoding="utf-8")

    assert await _say(bot, "/revert cancel") == "ℹ️ No /revert menu is open."
    await _say(bot, "/revert")
    assert await _say(bot, "/revert cancel") == "❌ Revert cancelled"
    await _say(bot, "/revert")
    await _say(bot, "1")
    assert await _say(bot, "5") == "❌ Revert cancelled"
    assert await _say(bot, "/revert 1 cancel") == "❌ Revert cancelled"
    assert bot._revert_choices == {}
    assert spy == [] and chat.cleared == [] and path.read_text(encoding="utf-8") == before


async def test_summary_mode_does_not_touch_the_transcript(
    tmp_path: Path, matrix_config: dict[str, Any], spy: list[tuple[Path, str, int]]
) -> None:
    bot, chat, _manager, path, _clock = _revert_bot(tmp_path)
    before = path.read_text(encoding="utf-8")
    assert (await _say(bot, "/revert 1 summary")).startswith("📋 Summarize-from-here does not rewrite")
    assert spy == [] and chat.cleared == [] and path.read_text(encoding="utf-8") == before


async def test_provider_gates_and_missing_session(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    for provider in ("piri", "danso"):
        bot, chat, _manager, _path, _clock = _revert_bot(tmp_path / provider, agent_provider=provider)
        assert await _say(bot, "/revert") == f"ℹ️ /revert is unavailable for {provider.title()} sessions."
        assert bot._revert_choices == {}

    scoped, _chat, _manager, _path, _clock = _revert_bot(
        tmp_path / "scoped", bridge_memory_mode="audience-scoped"
    )
    assert (await _say(scoped, "/revert")).startswith("🔒 Claude transcript revert is disabled")

    bare, _chat, manager, _path, _clock = _revert_bot(tmp_path / "bare")
    manager.rows.clear()
    assert await _say(bare, "/revert") == "📭 No active session. Start a conversation first."

    empty, _chat, _manager, _path, _clock = _revert_bot(tmp_path / "empty", users=[])
    assert await _say(empty, "/revert") == "📭 No conversation history available to revert."
    assert empty._revert_choices == {}


async def test_session_change_closes_the_menu(
    tmp_path: Path, matrix_config: dict[str, Any], spy: list[tuple[Path, str, int]]
) -> None:
    bot, chat, manager, _path, _clock = _revert_bot(tmp_path)
    await _say(bot, "/revert")
    await _say(bot, "1")
    _transcript(chat.conversations_dir, ["other"], session_id="s-1")
    manager.rows[bot.ids.user_id(OWNER)]["session_id"] = "s-1"
    assert await _say(bot, "2") == "❌ The session changed since /revert. Send /revert again."
    assert bot._revert_choices == {} and spy == []


async def test_pages_keep_absolute_numbers(
    tmp_path: Path, matrix_config: dict[str, Any], spy: list[tuple[Path, str, int]]
) -> None:
    users = [f"msg{n:02d}" for n in range(12)]
    bot, chat, _manager, _path, _clock = _revert_bot(tmp_path, users=users)

    first = await _say(bot, "/revert")
    assert "Page 1/2" in first and "10. 💬" in first and "11. 💬" not in first
    second = await _say(bot, "/revert page 2")
    assert "11. 💬" in second and "12. 💬" in second and "msg00" in second
    # 12 = the oldest message, line 0; picked from the stored listing.
    assert (await _say(bot, "12")).startswith("🔄 Selected message:\n\nmsg00")
    await _say(bot, "2")
    assert spy == [(chat.conversations_dir, SESSION, 0)]


async def test_revert_menu_supersedes_a_resume_list(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, _chat, manager, _path, _clock = _revert_bot(tmp_path)
    owner = bot.ids.user_id(OWNER)
    manager.rows[owner]["resume_list"] = [["s-9", "other", "claude"]]
    await _say(bot, "/revert")
    assert "resume_list" not in manager.rows[owner]
    assert (await _say(bot, "1")).startswith("🔄 Selected message:")


async def test_hande_inherits_the_revert_menu(
    tmp_path: Path, spy: list[tuple[Path, str, int]]
) -> None:
    from telegram_bot.core.hande.bot import HandeBot

    config = {
        "account": "@bridgenode:hande",
        "owner": "@owner:hande",
        "rooms": ["agent"],
        "family_rooms": [],
        "family_users": [],
        "socket_path": str(tmp_path / "responder.sock"),
    }
    config_path = tmp_path / "hande.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    os.chmod(config_path, 0o600)
    settings = _settings(
        tmp_path, agent_provider="claude", hande_config_path=config_path, hande_socket_path=None, channel="hande"
    )
    chat = RevertProjectChat(tmp_path / "conversations")
    path = _transcript(chat.conversations_dir, ["first", "second"])
    manager = FakeSessionManager(provider="claude")
    bot = HandeBot(settings, project_chat=chat, session_manager=manager, clock=SimpleNamespace(time=lambda: NOW))
    owner = bot.ids.user_id("@owner:hande")
    manager.rows[owner] = {"provider": "claude", "session_id": SESSION}

    async def say(body: str) -> str:
        job = {"event_id": "t1", "room_id": "agent", "sender": "@owner:hande", "scope": "ab" * 32, "body": body}
        return (await bot.run_turn(job, sink=FakeSink(), session_id=None, room_kind="direct")).text

    assert (await say("/revert")).startswith("🔄 Select a message to revert to:")
    assert (await say("1")).startswith("🔄 Selected message:\n\nsecond")
    assert (await say("2")).startswith("✅ Reverted to before:")
    assert spy == [(chat.conversations_dir, SESSION, 2)]
    assert len(_lines(path)) == 2


def test_shared_executor_rejects_paths_outside_the_conversations_dir(tmp_path: Path) -> None:
    path = _transcript(tmp_path / "conv", ["a", "b"])
    assert revert_ops.revert_conversation(tmp_path / "conv", "../escape", 1) is False
    assert revert_ops.revert_conversation(tmp_path / "conv", "missing", 1) is False
    assert revert_ops.revert_conversation(tmp_path / "conv", SESSION, 2) is True
    assert len(_lines(path)) == 2
