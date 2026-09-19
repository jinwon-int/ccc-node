"""MatrixBot frontend over ProjectChatHandler (#1780 PR-2b).

Everything the bot touches is faked at the handler / session-manager /
transport seam; ``telegram_bot.core.matrix.state`` (written in parallel) is
replaced by a stub module so ``load_config`` can be exercised lazily.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import time
import types
from types import SimpleNamespace
from typing import Any

import anyio
import pytest

from telegram_bot.contracts.agent_runtime import ModelInfo
from telegram_bot.core.agent_runtime import ApprovalDecision, ApprovalRequestEvent
from telegram_bot.core.matrix.bot import (
    DIRECT_ROOMS_FILENAME,
    IDS_FILENAME,
    MatrixBot,
    MatrixConfigError,
    MatrixTurnRunner,
)
from telegram_bot.core.matrix_ids import MatrixIdMap
from telegram_bot.core.project_chat_types import ChatResponse
from telegram_bot.core.session_scope import is_group_conversation
from telegram_bot.core.usage import UsageSnapshot

OWNER = "@owner:example.org"
KID = "@kid:example.org"
DM_ROOM = "!dm:example.org"
FAMILY_ROOM = "!family:example.org"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def matrix_config(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    config = {
        "homeserver": "https://matrix.example.org",
        "account": "@bridge:example.org",
        "device_id": "DEV",
        "owner": OWNER,
        "rooms": [DM_ROOM],
        "family_rooms": [FAMILY_ROOM],
        "family_users": [KID, OWNER],
        "not_before_ms": 0,
        "loaded_from": [],
    }
    module = types.ModuleType("telegram_bot.core.matrix.state")

    def load_config(path: Path) -> dict[str, Any]:
        config["loaded_from"].append(Path(path))
        return dict(config)

    module.load_config = load_config  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "telegram_bot.core.matrix.state", module)
    return config


def _settings(tmp_path: Path, **overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = dict(
        agent_provider="codex",
        project_root=tmp_path,
        execution_profile="owner-operator",
        bash_policy="approve-each",
        allowed_user_ids=[7],
        require_allowlist=True,
        claude_settings_path=tmp_path / "claude" / "settings.json",
        bot_data_dir=tmp_path / "data",
        matrix_config_path=tmp_path / "matrix.json",
        telegram_session_scope="per-user-chat",
        danso_model="gpt-6-astra",
        bridge_memory_mode="off",
        auto_new_session_after_hours=None,
        matrix_startup_banner=False,  # lifecycle tests assert exact notice lists
    )
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeProjectChat:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.response = ChatResponse(content="answer", session_id="s-new")
        self.models = [
            ModelInfo(
                id="gpt-5",
                display_name="GPT-5",
                default_reasoning_effort="medium",
                supported_reasoning_efforts=("low", "medium", "high"),
                is_default=True,
            ),
            ModelInfo(id="gpt-5-mini", display_name="GPT-5 mini"),
        ]
        self.models_error: Exception | None = None
        self.usage_calls: list[tuple[int, int, str | None]] = []
        self.stop_calls: list[tuple[int, int | None]] = []
        self.stop_result = True
        self.invalidated: list[tuple[int, int | None]] = []
        self.streaming_cancelled: list[tuple[int, int | None]] = []
        self.sender: Any = None
        self.usage_meter = None
        self.approval_active = True
        self.conversations_dir = None
        self._memory_route = "matrix"
        self.on_process: Any = None

    async def process_message(self, **kwargs: Any) -> ChatResponse:
        self.calls.append(kwargs)
        if self.on_process is not None:
            await self.on_process(kwargs)
        return self.response

    async def get_usage(self, user_id: int, chat_id: int, session_id: str | None) -> UsageSnapshot:
        self.usage_calls.append((user_id, chat_id, session_id))
        return UsageSnapshot(provider="codex")

    async def list_runtime_models(self) -> list[ModelInfo]:
        if self.models_error is not None:
            raise self.models_error
        return list(self.models)

    async def stop(self, user_id: int, chat_id: int | None = None) -> bool:
        self.stop_calls.append((user_id, chat_id))
        return self.stop_result

    async def cancel_user_streaming(self, user_id: int, chat_id: int | None = None) -> bool:
        self.streaming_cancelled.append((user_id, chat_id))
        return False

    def invalidate_agent_approvals(self, user_id: int, chat_id: int | None = None) -> None:
        self.invalidated.append((user_id, chat_id))

    def is_agent_approval_active(self, user_id: int, chat_id: int, generation: int) -> bool:
        return self.approval_active

    def set_async_completion_sender(self, sender: Any) -> None:
        self.sender = sender

    def render_cost_report(self, *, days: int = 7) -> str:
        return f"cost report {days}d"


class FakeSessionManager:
    def __init__(self, provider: str = "codex") -> None:
        self.provider = provider
        self.rows: dict[Any, dict[str, Any]] = {}
        self.patches: list[tuple[Any, dict[str, Any], set[str]]] = []
        self.auto_new = False
        self.last_user_message_at: list[tuple[Any, Any]] = []

    def _row(self, key: Any) -> dict[str, Any]:
        return self.rows.setdefault(key, {"provider": self.provider, "reply_mode": "text"})

    async def get_session(self, key: Any) -> dict[str, Any]:
        return dict(self._row(key))

    async def align_active_provider(self, key: Any) -> tuple[dict[str, Any], bool]:
        row = self._row(key)
        if row["provider"] == self.provider:
            return dict(row), False
        await self.patch_session(
            key,
            updates={"provider": self.provider, "session_id": None, "new_session": True},
            remove_fields={"model", "effort"},
        )
        return dict(row), True

    async def patch_session(
        self, key: Any, *, updates: Any = None, remove_fields: Any = ()
    ) -> None:
        row = self._row(key)
        row.update(dict(updates or {}))
        for field in remove_fields:
            row.pop(field, None)
        self.patches.append((key, dict(updates or {}), set(remove_fields)))

    async def patch_session_if(self, key: Any, *, expected: Any, updates: Any) -> bool:
        row = self._row(key)
        if all(row.get(k) == v for k, v in expected.items()):
            row.update(updates)
            return True
        return False

    async def should_start_new_session(self, key: Any, now: Any = None) -> bool:
        return self.auto_new

    async def set_last_user_message_at(self, key: Any, at: Any = None) -> None:
        self.last_user_message_at.append((key, at))


class FakeSink:
    def __init__(self, approve: Any = True) -> None:
        self.typing_calls = 0
        self.interims: list[str] = []
        self.statuses: list[str | None] = []
        self.approvals: list[tuple[str, Any]] = []
        self.approve = approve

    async def typing(self) -> None:
        self.typing_calls += 1

    async def interim(self, text: str) -> None:
        self.interims.append(text)

    async def status(self, text: str | None) -> None:
        self.statuses.append(text)

    async def approval(self, description: str, arguments: Any) -> bool:
        self.approvals.append((description, arguments))
        if isinstance(self.approve, Exception):
            raise self.approve
        return bool(self.approve)


class FakeTransport:
    def __init__(self, config: Any, runner: Any, *, script: Any = None, fail_run: bool = False) -> None:
        self.config = config
        self.runner = runner
        self.script = script
        self.fail_run = fail_run
        self.events: list[str] = []
        self.notices: list[tuple[str, str]] = []
        self.notice_keys: list[str | None] = []
        self.kinds: dict[str, str] = {DM_ROOM: "direct", FAMILY_ROOM: "family"}

    async def open(self, initialize: bool = False) -> None:
        self.events.append("open")

    async def run(self) -> None:
        self.events.append("run")
        if self.script is not None:
            await self.script(self)
        if self.fail_run:
            raise RuntimeError("sync loop died")

    async def close(self) -> None:
        self.events.append("close")

    def enqueue_notice(self, room_id: str, text: str, *, key: str | None = None) -> None:
        self.notices.append((room_id, text))
        self.notice_keys.append(key)

    def room_kind(self, room_id: str) -> str:
        return self.kinds.get(room_id, "family")


class FormattedTransport(FakeTransport):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.formatted: list[tuple[str, str, str | None]] = []

    async def send_formatted(self, room_id: str, body: str, formatted_body: str | None) -> None:
        self.formatted.append((room_id, body, formatted_body))


def _job(body: str, *, sender: str = OWNER, room: str = DM_ROOM, event_id: str = "$e1") -> dict[str, Any]:
    return {"event_id": event_id, "room_id": room, "sender": sender, "scope": "ab" * 32, "body": body}


def _bot(tmp_path: Path, **overrides: Any) -> tuple[MatrixBot, FakeProjectChat, FakeSessionManager]:
    settings = _settings(tmp_path, **overrides)
    chat = FakeProjectChat()
    manager = FakeSessionManager(provider=settings.agent_provider)
    bot = MatrixBot(settings, project_chat=chat, session_manager=manager, clock=SimpleNamespace(time=lambda: 1_700_000_000.0))
    return bot, chat, manager


async def _attach(bot: MatrixBot, transport_cls: type[FakeTransport] = FakeTransport) -> FakeTransport:
    """Run the lifecycle with a transport whose ``run`` hands control back to the test."""

    holder: dict[str, Any] = {}

    async def script(transport: FakeTransport) -> None:
        holder["transport"] = transport
        await holder["body"](transport)

    bot._transport_factory = lambda config, runner: transport_cls(config, runner, script=script)
    return holder  # type: ignore[return-value]


# --- configuration -----------------------------------------------------------


def test_missing_matrix_config_path_is_a_clear_error(tmp_path: Path) -> None:
    bot, _chat, _manager = _bot(tmp_path, matrix_config_path=None)
    with pytest.raises(MatrixConfigError, match="matrix_config_path"):
        bot.config_path()
    with pytest.raises(MatrixConfigError, match="matrix_config_path"):
        bot.validate_runtime_paths()
    settings = _settings(tmp_path)
    del settings.matrix_config_path
    bot = MatrixBot(settings, project_chat=FakeProjectChat(), session_manager=FakeSessionManager())
    with pytest.raises(MatrixConfigError, match="matrix_config_path"):
        bot.validate_runtime_paths()


def test_validate_runtime_paths(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, _chat, _manager = _bot(tmp_path)
    with pytest.raises(MatrixConfigError, match="not found"):
        bot.validate_runtime_paths()
    (tmp_path / "matrix.json").write_text("{}")
    bot.validate_runtime_paths()
    # A corrupt persisted id map fails closed instead of renumbering rooms.
    ids_path = tmp_path / "data" / IDS_FILENAME
    ids_path.parent.mkdir(parents=True)
    ids_path.write_text("{not json")
    bot, _chat, _manager = _bot(tmp_path)
    with pytest.raises(ValueError, match="matrix id map"):
        bot.validate_runtime_paths()


def test_load_config_is_lazy_and_cached(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, _chat, _manager = _bot(tmp_path)
    assert matrix_config["loaded_from"] == []
    assert bot.load_config()["owner"] == OWNER
    bot.load_config()
    assert matrix_config["loaded_from"] == [tmp_path / "matrix.json"]


def test_allowed_user_ints_covers_owner_and_family(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, _chat, _manager = _bot(tmp_path)
    ints = bot.allowed_user_ints()
    assert ints == [bot.ids.user_id(OWNER), bot.ids.user_id(KID)]  # deduped, owner first
    assert all(i > 1_000_000_000_000 for i in ints)
    # Persisted 0600 next to the other bot data.
    ids_path = tmp_path / "data" / IDS_FILENAME
    assert ids_path.exists()
    assert MatrixIdMap(ids_path).user_id(OWNER) == ints[0]


# --- identity mapping --------------------------------------------------------


@pytest.mark.anyio
async def test_direct_room_reports_sender_int_as_chat_id(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, chat, _manager = _bot(tmp_path)
    result = await bot.run_turn(_job("hello"), sink=FakeSink(), session_id=None, room_kind="direct")
    call = chat.calls[0]
    assert call["user_message"] == "hello"
    assert call["user_id"] == call["chat_id"] == bot.ids.user_id(OWNER)
    assert not is_group_conversation(call["user_id"], call["chat_id"])
    assert result.text == "answer"
    assert result.session_id == "s-new"
    assert result.streamed is False
    assert result.status == "complete"
    # The DM room is remembered (persisted) for outbound routing.
    assert bot.room_for_chat(call["chat_id"]) == DM_ROOM
    rooms_path = tmp_path / "data" / DIRECT_ROOMS_FILENAME
    assert json.loads(rooms_path.read_text())["rooms"] == {OWNER: DM_ROOM}
    fresh, _c, _m = _bot(tmp_path)
    assert fresh.room_for_chat(call["chat_id"]) == DM_ROOM


@pytest.mark.anyio
async def test_family_room_reports_room_int_shared_by_senders(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, chat, _manager = _bot(tmp_path)
    await bot.run_turn(_job("a", sender=OWNER, room=FAMILY_ROOM), sink=FakeSink(), session_id=None, room_kind="family")
    await bot.run_turn(_job("b", sender=KID, room=FAMILY_ROOM), sink=FakeSink(), session_id=None, room_kind="family")
    first, second = chat.calls
    assert first["chat_id"] == second["chat_id"] == bot.ids.room_id(FAMILY_ROOM)
    assert first["user_id"] != second["user_id"]
    assert is_group_conversation(first["user_id"], first["chat_id"])
    assert bot.room_for_chat(first["chat_id"]) == FAMILY_ROOM
    assert bot.room_for_chat(123) is None


# --- session resolution ------------------------------------------------------


@pytest.mark.anyio
async def test_session_comes_from_session_manager_not_transport_hint(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, chat, manager = _bot(tmp_path)
    user = bot.ids.user_id(OWNER)
    manager.rows[user] = {"provider": "codex", "session_id": "s-9", "model": "gpt-5", "effort": "high"}
    result = await bot.run_turn(_job("hi"), sink=FakeSink(), session_id="stale-transport-id", room_kind="direct")
    call = chat.calls[0]
    assert call["session_id"] == "s-9"
    assert call["new_session"] is False
    assert call["model"] == "gpt-5"
    assert call["effort"] == "high"
    assert call["usage_mode"] == "interactive"
    assert call["approval_policy"] == "untrusted"
    assert call["sandbox_policy"] is None
    # The handler's answer session is persisted and echoed back to the transport.
    assert manager.rows[user]["session_id"] == "s-new"
    assert result.session_id == "s-new"
    assert manager.last_user_message_at and manager.last_user_message_at[0][0] == user


@pytest.mark.anyio
async def test_new_session_flag_is_consumed_once(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, chat, manager = _bot(tmp_path)
    user = bot.ids.user_id(OWNER)
    manager.rows[user] = {"provider": "codex", "new_session": True}
    await bot.run_turn(_job("one"), sink=FakeSink(), session_id=None, room_kind="direct")
    await bot.run_turn(_job("two"), sink=FakeSink(), session_id=None, room_kind="direct")
    assert [c["new_session"] for c in chat.calls] == [True, False]
    assert manager.rows[user]["new_session"] is False


@pytest.mark.anyio
async def test_auto_new_session_resets_the_row(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, chat, manager = _bot(tmp_path)
    user = bot.ids.user_id(OWNER)
    manager.rows[user] = {"provider": "codex", "session_id": "old"}
    manager.auto_new = True
    await bot.run_turn(_job("x"), sink=FakeSink(), session_id=None, room_kind="direct")
    assert chat.calls[0]["new_session"] is True
    assert chat.calls[0]["session_id"] is None


@pytest.mark.anyio
async def test_failed_turn_does_not_persist_session(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, chat, manager = _bot(tmp_path)
    chat.response = ChatResponse(content="boom", success=False, error="x", session_id="s-err")
    result = await bot.run_turn(_job("x"), sink=FakeSink(), session_id=None, room_kind="direct")
    assert result.status == "error"
    assert "session_id" not in manager.rows[bot.ids.user_id(OWNER)]


# --- commands ----------------------------------------------------------------


@pytest.mark.anyio
async def test_cmd_new(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, chat, manager = _bot(tmp_path)
    user = bot.ids.user_id(OWNER)
    manager.rows[user] = {"provider": "codex", "session_id": "s-9", "effort": "high"}
    result = await bot.run_turn(_job("/new"), sink=FakeSink(), session_id=None, room_kind="direct")
    assert result.text.startswith("🆕 Switched to new session mode")
    assert "Codex" in result.text
    assert chat.calls == []
    row = manager.rows[user]
    assert row["session_id"] is None and row["new_session"] is True
    assert row["effort"] == "high"  # same provider: effort kept, as on Telegram
    assert chat.stop_calls == [(user, user)]  # running turn interrupted through the handler
    assert chat.invalidated == [(user, user)]


@pytest.mark.anyio
async def test_cmd_new_claude_syncs_settings_model(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, _chat, manager = _bot(tmp_path, agent_provider="claude")
    settings_path = tmp_path / "claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({"model": "opus"}))
    result = await bot.run_turn(_job("/new"), sink=FakeSink(), session_id=None, room_kind="direct")
    assert "Claude Code" in result.text
    assert manager.rows[bot.ids.user_id(OWNER)]["model"] == "opus"


@pytest.mark.anyio
async def test_cmd_model_list_and_select(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, chat, manager = _bot(tmp_path)
    user = bot.ids.user_id(OWNER)
    listing = await bot.run_turn(_job("/model"), sink=FakeSink(), session_id=None, room_kind="direct")
    assert "gpt-5 — GPT-5 (default)" in listing.text
    assert "gpt-5-mini — GPT-5 mini" in listing.text
    selected = await bot.run_turn(_job("/model gpt-5-mini"), sink=FakeSink(), session_id=None, room_kind="direct")
    assert selected.text == "✅ Switched to gpt-5-mini"
    assert manager.rows[user]["model"] == "gpt-5-mini"
    listing = await bot.run_turn(_job("/model"), sink=FakeSink(), session_id=None, room_kind="direct")
    assert "gpt-5-mini — GPT-5 mini (current)" in listing.text
    # Switching to a model that lacks the current effort resets it with a note.
    manager.rows[user]["effort"] = "high"
    selected = await bot.run_turn(_job("/model gpt-5-mini"), sink=FakeSink(), session_id=None, room_kind="direct")
    assert selected.text.startswith("✅ Switched to gpt-5-mini\nℹ️ Reasoning effort high is unsupported")
    assert "effort" not in manager.rows[user]
    chat.models_error = RuntimeError("down")
    listing = await bot.run_turn(_job("/model"), sink=FakeSink(), session_id=None, room_kind="direct")
    assert listing.text.startswith("⚠️ Codex model list is unavailable")
    assert chat.calls == []


@pytest.mark.anyio
async def test_cmd_model_claude_table_and_provider_guards(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, _chat, manager = _bot(tmp_path, agent_provider="claude")
    listing = await bot.run_turn(_job("/model"), sink=FakeSink(), session_id=None, room_kind="direct")
    assert "• sonnet — Claude Sonnet (current)" in listing.text
    assert "• opus — Claude Opus" in listing.text
    selected = await bot.run_turn(_job("/model opus"), sink=FakeSink(), session_id=None, room_kind="direct")
    assert selected.text == "✅ Switched to Claude Opus"
    assert manager.rows[bot.ids.user_id(OWNER)]["model"] == "opus"
    bot, _chat, _manager = _bot(tmp_path, agent_provider="danso")
    result = await bot.run_turn(_job("/model other"), sink=FakeSink(), session_id=None, room_kind="direct")
    assert result.text.startswith("❌ Danso uses the model configured by the operator")
    bot, _chat, _manager = _bot(tmp_path, agent_provider="piri")
    result = await bot.run_turn(_job("/model bad!id"), sink=FakeSink(), session_id=None, room_kind="direct")
    assert result.text.startswith("❌ Invalid Piri model id")


@pytest.mark.anyio
async def test_cmd_effort(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, chat, manager = _bot(tmp_path)
    user = bot.ids.user_id(OWNER)
    listing = await bot.run_turn(_job("/effort"), sink=FakeSink(), session_id=None, room_kind="direct")
    assert listing.text.startswith("🧠 Reasoning effort for GPT-5")
    assert "• medium (model default)" in listing.text
    assert "• default — use the model default" in listing.text
    result = await bot.run_turn(_job("/effort high"), sink=FakeSink(), session_id=None, room_kind="direct")
    assert result.text == "✅ Reasoning effort set to high for GPT-5"
    assert manager.rows[user]["effort"] == "high"
    result = await bot.run_turn(_job("/effort bogus"), sink=FakeSink(), session_id=None, room_kind="direct")
    assert result.text.startswith("❌ Unsupported effort for GPT-5: bogus")
    result = await bot.run_turn(_job("/effort default"), sink=FakeSink(), session_id=None, room_kind="direct")
    assert result.text.startswith("✅ Reasoning effort reset to model default (medium)")
    assert "effort" not in manager.rows[user]
    chat.models_error = RuntimeError("down")
    result = await bot.run_turn(_job("/effort"), sink=FakeSink(), session_id=None, room_kind="direct")
    assert result.text == "⚠️ Codex effort options are unavailable."
    bot, _chat, _manager = _bot(tmp_path, agent_provider="claude")
    result = await bot.run_turn(_job("/effort high"), sink=FakeSink(), session_id=None, room_kind="direct")
    assert result.text == "⚠️ /effort is available for Codex, Piri, or Danso."


@pytest.mark.anyio
async def test_cmd_usage(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, chat, manager = _bot(tmp_path)
    user = bot.ids.user_id(OWNER)
    manager.rows[user] = {"provider": "codex", "session_id": "s-9"}
    result = await bot.run_turn(_job("/usage"), sink=FakeSink(), session_id=None, room_kind="direct")
    assert chat.usage_calls == [(user, user, "s-9")]
    assert result.text.startswith("📊 Usage")
    assert result.text.endswith("cost report 7d")
    assert chat.calls == []


@pytest.mark.anyio
async def test_cmd_skills_runs_a_fresh_turn(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, chat, manager = _bot(tmp_path)
    sink = FakeSink()
    result = await bot.run_turn(_job("/skills"), sink=sink, session_id=None, room_kind="direct")
    call = chat.calls[0]
    assert call["new_session"] is True
    assert "List all installed skills" in call["user_message"]
    assert "HTML" in call["user_message"] and "Telegram" not in call["user_message"]
    assert call["typing_callback"] == sink.typing
    assert result.text == "answer"
    assert manager.rows[bot.ids.user_id(OWNER)]["session_id"] == "s-new"


@pytest.mark.anyio
async def test_cmd_stop_and_cancel_use_the_handler_stop_path(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, chat, _manager = _bot(tmp_path)
    user = bot.ids.user_id(OWNER)
    result = await bot.run_turn(_job("/stop"), sink=FakeSink(), session_id=None, room_kind="direct")
    assert result.text == "⏸️ Paused"
    assert chat.stop_calls == [(user, user)]
    assert chat.invalidated == [(user, user)]
    assert chat.streaming_cancelled == [(user, user)]
    chat.stop_result = False
    result = await bot.run_turn(_job("/STOP"), sink=FakeSink(), session_id=None, room_kind="direct")
    assert result.text == "ℹ️ Nothing running"
    assert chat.calls == []
    # cancel(job) without a transport falls back to the remembered DM room kind.
    chat.stop_result = True
    assert await bot.cancel(_job("ignored")) is True
    assert chat.stop_calls[-1] == (user, user)
    family = bot.ids.room_id(FAMILY_ROOM)
    assert await bot.cancel(_job("ignored", room=FAMILY_ROOM, sender=KID)) is True
    assert chat.stop_calls[-1] == (bot.ids.user_id(KID), family)


@pytest.mark.anyio
async def test_unknown_slash_command_is_forwarded_as_text(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, chat, _manager = _bot(tmp_path)
    await bot.run_turn(_job("/commit now"), sink=FakeSink(), session_id=None, room_kind="direct")
    assert chat.calls[0]["user_message"] == "/commit now"


# --- callback adapters -------------------------------------------------------


def _event() -> ApprovalRequestEvent:
    return ApprovalRequestEvent(request_id="r1", action="bash", arguments={"cmd": "ls"}, description="Run ls")


@pytest.mark.anyio
async def test_typing_status_and_interim_adapters(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, chat, _manager = _bot(tmp_path)
    sink = FakeSink()
    seen: dict[str, Any] = {}

    async def drive(kwargs: dict[str, Any]) -> None:
        await kwargs["typing_callback"]()
        seen["h1"] = await kwargs["status_callback"]("⏳ Working", None)
        # Same text again and a different text inside the throttle window are
        # both swallowed: heartbeats must not become a room message every 4 s.
        seen["h1b"] = await kwargs["status_callback"]("⏳ Working", seen["h1"])
        seen["h2"] = await kwargs["status_callback"]("⏳ Still working", seen["h1"])
        seen["deleted"] = await kwargs["status_callback"](None, seen["h2"])
        await kwargs["interim_message_callback"]("first part")

    chat.on_process = drive
    await bot.run_turn(_job("go"), sink=sink, session_id=None, room_kind="direct")
    assert sink.typing_calls == 1
    assert isinstance(seen["h1"], int) and seen["h1"] == seen["h2"] == seen["h1b"]
    assert seen["deleted"] is None
    assert sink.statuses == ["⏳ Working", None]  # same-window texts swallowed; None redacts
    assert sink.interims == ["first part"]


@pytest.mark.anyio
async def test_status_callback_forwards_new_text_after_interval(
    tmp_path: Path, matrix_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from telegram_bot.core.matrix import bot as bot_module

    bot, chat, _manager = _bot(tmp_path)
    sink = FakeSink()
    monkeypatch.setattr(bot_module, "STATUS_MIN_INTERVAL_S", 0.0)

    async def drive(kwargs: dict[str, Any]) -> None:
        await kwargs["status_callback"]("⏳ Working", None)
        await kwargs["status_callback"]("⏳ Working", None)  # identical text: still deduped
        await kwargs["status_callback"]("🔧 Running tests", None)

    chat.on_process = drive
    await bot.run_turn(_job("go"), sink=sink, session_id=None, room_kind="direct")
    assert sink.statuses == ["⏳ Working", "🔧 Running tests"]


@pytest.mark.anyio
async def test_status_callback_is_fail_open(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, chat, _manager = _bot(tmp_path)

    class BrokenSink(FakeSink):
        async def status(self, text: str | None) -> None:
            raise RuntimeError("room gone")

    seen: dict[str, Any] = {}

    async def drive(kwargs: dict[str, Any]) -> None:
        seen["handle"] = await kwargs["status_callback"]("⏳", None)

    chat.on_process = drive
    await bot.run_turn(_job("go"), sink=BrokenSink(), session_id=None, room_kind="direct")
    assert isinstance(seen["handle"], int)


@pytest.mark.parametrize(
    ("approve", "active", "sender", "expected", "prompted"),
    [
        (True, True, OWNER, ApprovalDecision.ALLOW, True),
        (False, True, OWNER, ApprovalDecision.DENY, True),
        (RuntimeError("ui"), True, OWNER, ApprovalDecision.DENY, True),
        (True, False, OWNER, ApprovalDecision.DENY, False),  # stale generation: fail closed
        (True, True, KID, ApprovalDecision.DENY, False),  # only the owner approves
    ],
)
@pytest.mark.anyio
async def test_approval_adapter(
    tmp_path: Path,
    matrix_config: dict[str, Any],
    approve: Any,
    active: bool,
    sender: str,
    expected: ApprovalDecision,
    prompted: bool,
) -> None:
    bot, chat, _manager = _bot(tmp_path)
    chat.approval_active = active
    sink = FakeSink(approve=approve)
    seen: dict[str, Any] = {}

    async def drive(kwargs: dict[str, Any]) -> None:
        seen["decision"] = await kwargs["approval_callback"](kwargs["chat_id"], kwargs["user_id"], _event(), 3)

    chat.on_process = drive
    await bot.run_turn(_job("go", sender=sender, room=FAMILY_ROOM), sink=sink, session_id=None, room_kind="family")
    assert seen["decision"] is expected
    assert bool(sink.approvals) is prompted
    if prompted:
        assert sink.approvals == [("Run ls", {"cmd": "ls"})]


@pytest.mark.parametrize(
    ("bash_policy", "expected"),
    [("auto-approve", ApprovalDecision.ALLOW), ("disabled", ApprovalDecision.DENY)],
)
@pytest.mark.anyio
async def test_approval_adapter_honours_bash_policy_without_prompting(
    tmp_path: Path, matrix_config: dict[str, Any], bash_policy: str, expected: ApprovalDecision
) -> None:
    bot, chat, _manager = _bot(tmp_path, bash_policy=bash_policy)
    sink = FakeSink()
    seen: dict[str, Any] = {}

    async def drive(kwargs: dict[str, Any]) -> None:
        seen["decision"] = await kwargs["approval_callback"](kwargs["chat_id"], kwargs["user_id"], _event(), 1)

    chat.on_process = drive
    await bot.run_turn(_job("go"), sink=sink, session_id=None, room_kind="direct")
    assert seen["decision"] is expected
    assert sink.approvals == []


# --- outbound routing: notification_bot + async completion sender -------------


@pytest.mark.anyio
async def test_notification_bot_reverse_maps_chat_id_to_room(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, chat, _manager = _bot(tmp_path)
    holder = await _attach(bot)

    async def body(transport: FakeTransport) -> None:
        # DM: chat_id is the *user* int, so the remembered DM room is used.
        await bot.run_turn(_job("hi"), sink=FakeSink(), session_id=None, room_kind="direct")
        route = chat.calls[-1]["notification_bot"]
        await route.send_message(chat_id=chat.calls[-1]["chat_id"], text="dm notice")
        await bot.run_turn(_job("hi", room=FAMILY_ROOM, sender=KID), sink=FakeSink(), session_id=None, room_kind="family")
        route = chat.calls[-1]["notification_bot"]
        await route.send_message(chat_id=chat.calls[-1]["chat_id"], text="family notice")
        with pytest.raises(RuntimeError, match="no Matrix route"):
            await route.send_message(chat_id=42, text="nowhere")

    holder["body"] = body
    await bot.serve()
    assert holder["transport"].notices == [(DM_ROOM, "dm notice"), (FAMILY_ROOM, "family notice")]


@pytest.mark.anyio
async def test_async_completion_sender(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, chat, _manager = _bot(tmp_path)
    holder = await _attach(bot)
    results: list[bool] = []

    async def body(transport: FakeTransport) -> None:
        assert chat.sender == bot.async_completion_sender  # wired before open/run
        await bot.run_turn(_job("hi"), sink=FakeSink(), session_id=None, room_kind="direct")
        user = bot.ids.user_id(OWNER)
        results.append(await chat.sender(user, user, "done: PR merged"))
        results.append(await chat.sender(user, 999, "unknown chat"))

    holder["body"] = body
    await bot.serve()
    assert results == [True, False]
    assert holder["transport"].notices == [(DM_ROOM, "done: PR merged")]
    # After the transport is closed there is no delivery path.
    user = bot.ids.user_id(OWNER)
    assert await chat.sender(user, user, "late") is False


# --- streaming / formatted delivery ------------------------------------------


@pytest.mark.anyio
async def test_streamed_response_passes_through(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, chat, _manager = _bot(tmp_path)
    chat.response = ChatResponse(content="", session_id="s-2", streamed=True)
    result = await bot.run_turn(_job("go"), sink=FakeSink(), session_id=None, room_kind="direct")
    assert result.streamed is True
    assert result.text == ""
    assert result.session_id == "s-2"


@pytest.mark.anyio
async def test_plain_transport_gets_plain_text(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, chat, _manager = _bot(tmp_path)
    holder = await _attach(bot)
    chat.response = ChatResponse(content="**bold**", session_id="s-1")
    sink = FakeSink()

    async def drive(kwargs: dict[str, Any]) -> None:
        await kwargs["interim_message_callback"]("*part*")

    chat.on_process = drive
    seen: dict[str, Any] = {}

    async def body(transport: FakeTransport) -> None:
        seen["result"] = await bot.run_turn(_job("go"), sink=sink, session_id=None, room_kind="direct")

    holder["body"] = body
    await bot.serve()
    assert seen["result"].text == "**bold**"
    assert seen["result"].streamed is False
    assert sink.interims == ["*part*"]


@pytest.mark.anyio
async def test_formatted_transport_gets_rendered_html(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, chat, _manager = _bot(tmp_path)
    holder = await _attach(bot, FormattedTransport)
    chat.response = ChatResponse(content="**bold** <x>", session_id="s-1")
    sink = FakeSink()

    async def drive(kwargs: dict[str, Any]) -> None:
        await kwargs["interim_message_callback"]("*part*")
        await kwargs["interim_message_callback"]("plain")

    chat.on_process = drive
    seen: dict[str, Any] = {}

    async def body(transport: FakeTransport) -> None:
        seen["result"] = await bot.run_turn(_job("go"), sink=sink, session_id=None, room_kind="direct")

    holder["body"] = body
    await bot.serve()
    transport = holder["transport"]
    assert transport.formatted == [
        (DM_ROOM, "*part*", "<p><em>part</em></p>"),
        (DM_ROOM, "plain", None),
        (DM_ROOM, "**bold** <x>", "<p><strong>bold</strong> &lt;x&gt;</p>"),
    ]
    assert sink.interims == []
    assert seen["result"].text == "**bold** <x>"
    assert seen["result"].streamed is True  # already delivered; transport must not resend


@pytest.mark.anyio
async def test_formatted_send_failure_falls_back_to_plain(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    class Flaky(FormattedTransport):
        async def send_formatted(self, room_id: str, body: str, formatted_body: str | None) -> None:
            raise RuntimeError("encrypt failed")

    bot, chat, _manager = _bot(tmp_path)
    holder = await _attach(bot, Flaky)
    chat.response = ChatResponse(content="**bold**", session_id="s-1")
    sink = FakeSink()

    async def drive(kwargs: dict[str, Any]) -> None:
        await kwargs["interim_message_callback"]("*part*")

    chat.on_process = drive
    seen: dict[str, Any] = {}

    async def body(transport: FakeTransport) -> None:
        seen["result"] = await bot.run_turn(_job("go"), sink=sink, session_id=None, room_kind="direct")

    holder["body"] = body
    await bot.serve()
    assert sink.interims == ["*part*"]
    assert seen["result"].streamed is False
    assert seen["result"].text == "**bold**"


# --- lifecycle ---------------------------------------------------------------


@pytest.mark.anyio
async def test_run_lifecycle_with_transport_factory(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, chat, _manager = _bot(tmp_path)
    built: list[tuple[Any, Any]] = []

    def factory(config: Any, runner: Any) -> FakeTransport:
        built.append((config, runner))
        return FakeTransport(config, runner)

    bot._transport_factory = factory
    await bot.serve()
    (config, runner), = built
    assert config["owner"] == OWNER
    assert isinstance(runner, MatrixTurnRunner)
    assert callable(runner.run) and callable(runner.cancel)
    assert chat.sender == bot.async_completion_sender
    assert built[0][1] is not None


@pytest.mark.anyio
async def test_run_closes_transport_when_run_fails(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, _chat, _manager = _bot(tmp_path)
    transports: list[FakeTransport] = []

    def factory(config: Any, runner: Any) -> FakeTransport:
        transport = FakeTransport(config, runner, fail_run=True)
        transports.append(transport)
        return transport

    bot._transport_factory = factory
    with pytest.raises(RuntimeError, match="sync loop died"):
        await bot.serve()
    assert transports[0].events == ["open", "run", "close"]


@pytest.mark.anyio
async def test_turn_runner_delegates_to_bot(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, chat, _manager = _bot(tmp_path)
    runner = bot.runner
    result = await runner.run(_job("via runner"), sink=FakeSink(), session_id=None, room_kind="direct")
    assert result.text == "answer"
    assert chat.calls[0]["user_message"] == "via runner"
    assert await runner.cancel(_job("x")) is True


def test_run_is_the_blocking_entry_main_expects(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    """``__main__.main()`` calls ``bot.run()`` synchronously; it must drive ``serve``."""

    bot, chat, manager = _bot(tmp_path)
    transports: list[FakeTransport] = []
    initialised: list[bool] = []
    manager.initialize = lambda: initialised.append(True)  # type: ignore[attr-defined]

    async def script(transport: FakeTransport) -> None:
        transports.append(transport)

    bot._transport_factory = lambda config, runner: FakeTransport(config, runner, script=script)
    result = bot.run()  # blocks until the fake transport's run() returns
    assert result is None
    assert initialised == [True]
    assert transports and transports[0].events == ["open", "run", "close"]
    assert chat.sender == bot.async_completion_sender


@pytest.mark.anyio
async def test_startup_banner_is_posted_to_direct_rooms_only(
    tmp_path: Path, matrix_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text('model = "gpt-6-astra"\nmodel_reasoning_effort = "high"\n')
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    bot, _chat, _manager = _bot(tmp_path, matrix_startup_banner=True)
    monkeypatch.setattr(type(bot), "_bridge_revision", staticmethod(lambda: "abc1234"))
    holder = await _attach(bot)

    async def body(transport: FakeTransport) -> None:
        return None

    holder["body"] = body
    await bot.serve()
    notices = holder["transport"].notices
    assert [room for room, _ in notices] == [DM_ROOM], "family rooms never get the banner"
    text = notices[0][1]
    assert text.startswith("🟢 ") and "ccc-node Matrix 프론트엔드 기동" in text
    assert " · codex · gpt-6-astra · high · abc1234" in text
    # Missing codex config just drops the model/effort parts.
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "nowhere"))
    assert bot.startup_banner().endswith(" · codex · abc1234")


@pytest.mark.anyio
async def test_initialize_flag_opens_the_store_once_and_does_not_serve(
    tmp_path: Path, matrix_config: dict[str, Any]
) -> None:
    bot, _chat, _manager = _bot(tmp_path, matrix_initialize=True, matrix_startup_banner=True)
    opened: list[bool] = []

    class InitTransport(FakeTransport):
        async def open(self, initialize: bool = False) -> None:
            opened.append(initialize)
            self.events.append("open")

    transports: list[FakeTransport] = []

    def factory(config: Any, runner: Any) -> FakeTransport:
        transport = InitTransport(config, runner, fail_run=True)  # run() must never be reached
        transports.append(transport)
        return transport

    bot._transport_factory = factory
    await bot.serve()
    transport = transports[0]
    assert opened == [True]
    assert transport.events == ["open", "close"], "initialize exits before run() and posts no banner"
    assert transport.notices == []


@pytest.mark.anyio
async def test_startup_banner_key_is_stable_across_restarts(
    tmp_path: Path, matrix_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash-looping unit must not queue one banner per restart: same key within the hour."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "nowhere"))
    bot, _chat, _manager = _bot(tmp_path, matrix_startup_banner=True)
    monkeypatch.setattr(type(bot), "_bridge_revision", staticmethod(lambda: "abc1234"))
    keys: list[str | None] = []
    for _ in range(2):
        holder = await _attach(bot)

        async def body(transport: FakeTransport) -> None:
            return None

        holder["body"] = body
        await bot.serve()
        keys.extend(holder["transport"].notice_keys)
    assert len(keys) == 2 and keys[0] == keys[1] and keys[0].startswith("startup-")


# --- turn-age watchdog wiring (#1825) ----------------------------------------


@pytest.mark.anyio
async def test_turn_age_watchdog_notifies_the_room_through_the_matrix_sender(
    tmp_path: Path, matrix_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale Matrix turn produces an age notice (#1825).

    Telegram gets this from BotLifecycleMixin, which MatrixBot does not inherit,
    so before this wiring a Matrix turn that went quiet emitted no signal at all.
    """

    monkeypatch.setenv("CCC_TURN_AGE_NOTIFY_MIN", "30")
    bot, chat, _manager = _bot(tmp_path)
    holder = await _attach(bot)
    user = bot.ids.user_id(OWNER)
    # One turn, registered 45 monotonic minutes ago. The stamp is frozen here
    # rather than recomputed per call: the watchdog reads its clock *before*
    # the provider, so a live `time.monotonic()` inside the lambda would make
    # the turn look a second younger than intended and round down to 44.
    started = time.monotonic() - 45 * 60.0
    chat._agent_session_registry = SimpleNamespace(
        active_turn_ages=lambda: (((user, user), started),)
    )

    async def body(transport: FakeTransport) -> None:
        # One real turn first: that is what registers the chat_id -> room
        # mapping the notice path resolves against.
        await bot.run_turn(_job("hi"), sink=FakeSink(), session_id=None, room_kind="direct")
        watchdog = bot._build_turn_age_watchdog()
        assert watchdog is not None
        await watchdog._tick()

    holder["body"] = body
    await bot.serve()
    assert len(holder["transport"].notices) == 1
    room, text = holder["transport"].notices[0]
    assert room == DM_ROOM
    assert "45 min" in text


@pytest.mark.anyio
async def test_turn_age_watchdog_is_disabled_by_zero_and_without_a_registry(
    tmp_path: Path, matrix_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, chat, _manager = _bot(tmp_path)
    chat._agent_session_registry = SimpleNamespace(active_turn_ages=tuple)

    monkeypatch.setenv("CCC_TURN_AGE_NOTIFY_MIN", "0")
    assert bot._build_turn_age_watchdog() is None

    monkeypatch.setenv("CCC_TURN_AGE_NOTIFY_MIN", "30")
    assert bot._build_turn_age_watchdog() is not None
    # A handler without the registry seam must degrade, not crash the frontend.
    del chat._agent_session_registry
    assert bot._build_turn_age_watchdog() is None


@pytest.mark.anyio
async def test_serve_actually_launches_the_turn_age_watchdog(
    tmp_path: Path, matrix_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """serve() must run the watchdog, not merely be able to build one (#1825).

    Driving ``_tick()`` by hand proves the builder and the delivery seam but
    still passes if serve() never launches the leg — this test fails when the
    ``group.create_task(watchdog.run(...))`` wiring is removed.
    """

    monkeypatch.setenv("CCC_TURN_AGE_NOTIFY_MIN", "30")
    bot, chat, _manager = _bot(tmp_path)
    holder = await _attach(bot)
    user = bot.ids.user_id(OWNER)
    started = time.monotonic() - 45 * 60.0
    chat._agent_session_registry = SimpleNamespace(
        active_turn_ages=lambda: (((user, user), started),)
    )

    build = bot._build_turn_age_watchdog

    def fast_watchdog() -> Any:
        watchdog = build()
        if watchdog is not None:
            watchdog._tick_seconds = 0.01  # keep the test off the 60s cadence
        return watchdog

    bot._build_turn_age_watchdog = fast_watchdog  # type: ignore[method-assign]

    async def body(transport: FakeTransport) -> None:
        # Registers the chat_id -> room mapping; until it exists the notice
        # cannot be delivered and the watchdog keeps retrying.
        await bot.run_turn(_job("hi"), sink=FakeSink(), session_id=None, room_kind="direct")
        with anyio.fail_after(5):
            while not transport.notices:
                await anyio.sleep(0.01)

    holder["body"] = body
    await bot.serve()
    assert holder["transport"].notices
    assert "Turn has been active" in holder["transport"].notices[0][1]


@pytest.mark.anyio
async def test_serve_stops_the_watchdog_when_the_transport_returns(
    tmp_path: Path, matrix_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """serve() must terminate even though the watchdog loops forever (#1825).

    A TaskGroup only cancels siblings when a leg *raises*, so a transport that
    returns cleanly would hang the group on the watchdog without the stop event.
    """

    monkeypatch.setenv("CCC_TURN_AGE_NOTIFY_MIN", "30")
    bot, chat, _manager = _bot(tmp_path)
    holder = await _attach(bot)
    chat._agent_session_registry = SimpleNamespace(active_turn_ages=tuple)

    async def body(transport: FakeTransport) -> None:
        return  # transport.run() returns normally

    holder["body"] = body
    with anyio.fail_after(5):
        await bot.serve()
