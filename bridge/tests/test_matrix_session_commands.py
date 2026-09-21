"""Matrix frontend: ``/history`` and ``/resume`` (#1895 PR-B) — Telegram's
provider rules over plain text, with the digit reply switching sessions."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from telegram_bot.core.matrix.bot import MatrixBot
from test_matrix_bot import FakeProjectChat, FakeSink, _bot, _job
from test_matrix_bot import matrix_config as _shared  # noqa: F401 - fixture registration below

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def matrix_config(_shared: dict[str, Any]) -> dict[str, Any]:  # noqa: F811 - re-exposed under its own name
    return _shared


class SessionProjectChat(FakeProjectChat):
    def __init__(self) -> None:
        super().__init__()
        self.recent = [
            {"role": "user", "content": "hello there", "timestamp": "2026-09-21T01:02:03Z"},
            {"role": "assistant", "content": "x" * 600, "timestamp": "bad-ts"},
        ]
        self.runtime_history = SimpleNamespace(messages=[
            SimpleNamespace(role="user", content="codex q", timestamp="2026-09-21T01:02:03+00:00"),
            SimpleNamespace(role="assistant", content="codex a", timestamp=None),
        ])
        self.runtime_sessions = [
            SimpleNamespace(id="t1", title="First thread", preview="", model="gpt-5", cwd="/w"),
            SimpleNamespace(id="t2", title="", preview="see https://x.test/a preview text", model=None, cwd=None),
        ]
        self.claude_sessions = [("s1", "Fix the build\nnow", 1_700_000_000.0 - 30), ("s2", "Plan trip", 1_700_000_000.0 - 7200)]

    def get_recent_messages(self, session_id: str, limit: int = 5) -> list[dict[str, Any]]:
        return list(self.recent)

    async def read_runtime_session(self, session_id: str, *, limit: int = 5) -> Any:
        return self.runtime_history

    async def list_runtime_sessions(self, *, limit: int = 10) -> list[Any]:
        return list(self.runtime_sessions)

    def list_sessions(self, limit: int = 10) -> list[tuple[str, str, float]]:
        return list(self.claude_sessions)

    def get_session_last_assistant_message(self, session_id: str) -> str | None:
        return "last answer" if session_id == "s1" else None


async def _prepared(tmp_path: Path, provider: str, *, session_id: str | None = "sid", **overrides: Any):
    bot, _chat, manager = _bot(tmp_path, agent_provider=provider, **overrides)
    chat = SessionProjectChat()
    bot._project_chat = chat
    user_id, chat_id, _room = bot._job_identity(_job("x"), "direct")
    key = bot._conversation_key(user_id, chat_id)
    if session_id:
        await manager.patch_session(key, updates={"provider": provider, "session_id": session_id})
        bot._runtime_active_sessions.add(key)
    return bot, chat, manager, key


async def _turn(bot: MatrixBot, body: str) -> Any:
    return await bot.runner.run(_job(body), sink=FakeSink(), session_id=None, room_kind="direct")


@pytest.mark.anyio
async def test_history_without_session_and_for_id_only_providers(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, _c, _m, _k = await _prepared(tmp_path, "codex", session_id=None)
    assert (await _turn(bot, "/history")).text.startswith("📭 No active session")
    bot, _c, _m, _k = await _prepared(tmp_path / "danso", "danso")
    assert "Danso does not expose bounded transcript history" in (await _turn(bot, "/history")).text


@pytest.mark.anyio
async def test_history_renders_claude_and_runtime_transcripts(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, _c, _m, _k = await _prepared(tmp_path, "claude")
    text = (await _turn(bot, "/history")).text
    assert text.startswith("📜 Recent History (last 5 messages)\nProvider: claude")
    assert "🧑 User [2026-09-21 01:02:03]\nhello there" in text
    assert "🤖 Assistant [bad-ts]" in text and "x" * 500 + "..." in text and "x" * 501 not in text
    bot, _c, _m, _k = await _prepared(tmp_path / "codex", "codex")
    text = (await _turn(bot, "/history")).text
    assert "Provider: codex" in text and "🧑 User [2026-09-21 01:02:03]\ncodex q" in text and "🤖 Assistant []\ncodex a" in text


@pytest.mark.anyio
async def test_resume_lists_runtime_threads_and_digit_switches(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, chat, manager, key = await _prepared(tmp_path, "codex")
    text = (await _turn(bot, "/resume")).text
    assert "1. First thread [codex · gpt-5 · /w]" in text and "2. see preview text [codex]" in text
    assert text.endswith("Reply with a number to switch to that session:")
    assert (await manager.get_session(key))["resume_list"] == [["t1", "First thread", "codex"], ["t2", "see preview text", "codex"]]
    assert (await _turn(bot, "9")).text == "❌ Invalid number, please try again."
    assert (await _turn(bot, "2")).text == "✅ Switched to session: see preview text"
    session = await manager.get_session(key)
    assert session["session_id"] == "t2" and "resume_list" not in session and key in bot._runtime_active_sessions
    assert chat.calls == []  # no provider turn was spent on the choice
    await _turn(bot, "2")  # no list pending any more: ordinary message
    assert chat.calls and chat.calls[-1]["user_message"] == "2"


@pytest.mark.anyio
async def test_non_digit_reply_clears_the_pending_list_and_runs_normally(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, chat, manager, key = await _prepared(tmp_path, "codex")
    await _turn(bot, "/resume")
    await _turn(bot, "what else?")
    assert chat.calls[-1]["user_message"] == "what else?"
    assert "resume_list" not in await manager.get_session(key)


@pytest.mark.anyio
async def test_claude_resume_is_locked_under_audience_scoped_memory_and_lists_otherwise(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, _c, manager, key = await _prepared(tmp_path, "claude", bridge_memory_mode="audience-scoped")
    await manager.patch_session(key, updates={"resume_list": [["s1", "old", "claude"]]})
    assert (await _turn(bot, "/resume")).text.startswith("🔒 Claude session browsing is disabled")
    assert "resume_list" not in await manager.get_session(key)
    bot, chat, manager, key = await _prepared(tmp_path / "open", "claude", bridge_memory_mode="off")
    text = (await _turn(bot, "/resume")).text
    assert "1. Fix the build now [claude]\n30 seconds ago" in text and "2. Plan trip [claude]\n2 hours ago" in text
    assert (await _turn(bot, "1")).text == "✅ Switched to session: Fix the build\nnow\n\n📋 last answer"
    assert (await manager.get_session(key))["session_id"] == "s1"


@pytest.mark.anyio
async def test_resume_for_danso_and_piri_reports_or_selects_exact_ids(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, _c, _m, _k = await _prepared(tmp_path, "danso")
    assert (await _turn(bot, "/resume")).text == "ℹ️ Current Danso session auto-resumes: sid"
    assert (await _turn(bot, "/resume other")).text.startswith("❌ Danso can resume only")
    bot, _c, manager, key = await _prepared(tmp_path / "piri", "piri")
    assert (await _turn(bot, "/resume")).text.startswith("ℹ️ Current Piri session auto-resumes: sid")
    assert (await _turn(bot, "/resume bad id!")).text in {"Usage: /resume <piri-session-id>", "❌ Invalid Piri session id."}
    assert (await _turn(bot, "/resume abc-123")).text == "✅ Piri session selected: abc-123"
    assert (await manager.get_session(key))["session_id"] == "abc-123"
