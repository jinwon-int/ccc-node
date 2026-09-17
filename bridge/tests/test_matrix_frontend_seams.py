"""Matrix frontend groundwork (#1780): transport-neutral seams.

Three seams a non-Telegram frontend needs, each pinned so Telegram behaviour
stays byte-identical:

* ``process_message(streaming_sink=...)`` replaces the Telegram draft editor;
* ``resolve_memory_audience(route=...)`` namespaces private memory per frontend
  (default digest unchanged);
* ``MatrixIdMap`` yields stable positive ints with the DM invariant.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
import hashlib
import hmac
import json
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest

from telegram_bot.core.agent_runtime import (
    AgentEvent,
    ApprovalHandler,
    CompletionEvent,
    SessionRequest,
    TextDeltaEvent,
    ToolStartedEvent,
    deny_approval,
)
from telegram_bot.core.matrix_ids import MatrixIdMap
from telegram_bot.core.memory_audience import load_or_create_audience_key, resolve_memory_audience
from telegram_bot.core.project_chat import ProjectChatHandler
from telegram_bot.core.session_scope import is_group_conversation
from telegram_bot.core.streaming_sink import StreamingSinkPort, validate_streaming_sink


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _settings(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        agent_provider="codex",
        project_root=tmp_path,
        execution_profile="strict-project",
        bash_policy="disabled",
        allowed_user_ids=[7],
        require_allowlist=True,
        claude_cli_path=None,
        claude_settings_path=tmp_path / "claude" / "settings.json",
        enable_streaming=True,  # would build the Telegram editor if a bot were passed
        enable_partial_streaming=False,
        bot_data_dir=None,
        task_ledger_path=None,
        session_guard_enabled=False,
    )


class _Session:
    def __init__(self, events: list[AgentEvent]) -> None:
        self.session_id = "s-1"
        self.events = events

    def send_turn(self, message: str, *, approval_handler: ApprovalHandler = deny_approval) -> AsyncIterator[AgentEvent]:
        async def stream() -> AsyncIterator[AgentEvent]:
            for event in self.events:
                yield event

        return stream()

    async def interrupt(self) -> None:  # pragma: no cover - not exercised
        return None


class _Runtime:
    def __init__(self, events: list[AgentEvent]) -> None:
        self.events = events

    async def start_or_resume(self, request: SessionRequest) -> _Session:
        return _Session(self.events)

    async def close(self) -> None:
        return None


class RecordingSink:
    """Minimal StreamingSinkPort that records the handler's calls."""

    def __init__(self, *, streamed: bool = True) -> None:
        self.calls: list[tuple[str, object]] = []
        self.streamed = streamed

    async def update_if_needed(self, new_text_chunk: str) -> bool:
        self.calls.append(("update", new_text_chunk))
        return self.streamed

    async def add_tool_call(self, name: str, input: dict) -> bool:
        self.calls.append(("tool", name))
        return self.streamed

    async def finalize_segment(self) -> bool:
        self.calls.append(("segment", None))
        return self.streamed

    async def finalize_all(self) -> bool:
        self.calls.append(("final", None))
        return self.streamed

    async def cancel(self) -> bool:
        self.calls.append(("cancel", None))
        return True


def _handler(tmp_path: Path, runtime: _Runtime, **kwargs) -> ProjectChatHandler:
    handler = ProjectChatHandler(settings=_settings(tmp_path), agent_runtime=runtime, **kwargs)
    handler._task_ledger_cache = False
    return handler


@pytest.mark.anyio
async def test_injected_sink_receives_deltas_tools_and_final(tmp_path: Path) -> None:
    runtime = _Runtime([TextDeltaEvent("hel"), ToolStartedEvent("t1", "shell", {"cmd": "ls"}), TextDeltaEvent("lo"), CompletionEvent("end_turn")])
    sink = RecordingSink()
    response = await _handler(tmp_path, runtime).process_message("hi", user_id=7, chat_id=7, streaming_sink=sink)
    kinds = [kind for kind, _ in sink.calls]
    assert kinds[:3] == ["update", "tool", "update"]
    assert kinds[-1] == "final"
    assert response.success and response.streamed, "a sink that delivered marks the reply as already streamed"
    assert isinstance(sink, StreamingSinkPort)


@pytest.mark.anyio
async def test_sink_that_delivered_nothing_leaves_reply_to_caller(tmp_path: Path) -> None:
    runtime = _Runtime([TextDeltaEvent("plain"), CompletionEvent("end_turn")])
    sink = RecordingSink(streamed=False)
    response = await _handler(tmp_path, runtime).process_message("hi", user_id=7, chat_id=7, streaming_sink=sink)
    assert response.success and not response.streamed
    assert response.content == "plain"


@pytest.mark.anyio
async def test_partial_sink_is_rejected_before_the_turn_starts(tmp_path: Path) -> None:
    class Partial:
        async def update_if_needed(self, chunk: str) -> bool:
            return True

    with pytest.raises(TypeError, match="finalize_all"):
        validate_streaming_sink(Partial())
    runtime = _Runtime([TextDeltaEvent("x"), CompletionEvent("end_turn")])
    with pytest.raises(TypeError):
        await _handler(tmp_path, runtime).process_message("hi", user_id=7, chat_id=7, streaming_sink=Partial())


@pytest.mark.anyio
async def test_no_sink_and_no_bot_keeps_legacy_non_streaming_path(tmp_path: Path) -> None:
    runtime = _Runtime([TextDeltaEvent("legacy"), CompletionEvent("end_turn")])
    response = await _handler(tmp_path, runtime).process_message("hi", user_id=7, chat_id=7)
    assert response.content == "legacy" and not response.streamed


def _audience_settings(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        bridge_memory_mode="audience-scoped",
        telegram_session_scope="per-user-chat",
        bot_data_dir=tmp_path,
        claude_settings_path=tmp_path / ".claude" / "settings.json",
        hook_policy_environment=lambda: {},
    )


def test_default_route_keeps_historical_telegram_digest(tmp_path: Path) -> None:
    settings = _audience_settings(tmp_path)
    audience = resolve_memory_audience(settings, user_id=42, chat_id=42)
    assert audience is not None
    key = load_or_create_audience_key(settings)
    expected = hmac.new(key, b"telegram-dm-user\x0042", hashlib.sha256).hexdigest()[:32]
    assert audience.scope == f"private-{expected}"
    assert resolve_memory_audience(settings, user_id=42, chat_id=42, route="telegram").scope == audience.scope


def test_matrix_route_never_collides_with_telegram_private_scope(tmp_path: Path) -> None:
    settings = _audience_settings(tmp_path)
    telegram = resolve_memory_audience(settings, user_id=42, chat_id=42)
    matrix = resolve_memory_audience(settings, user_id=42, chat_id=42, route="matrix")
    assert telegram is not None and matrix is not None
    assert telegram.scope != matrix.scope
    # Groups stay on the shared audience regardless of route.
    assert resolve_memory_audience(settings, user_id=42, chat_id=99, route="matrix").scope == "shared"
    for bad in ("", "Matrix", "tele gram", "x" * 40, None):
        with pytest.raises(ValueError):
            resolve_memory_audience(settings, user_id=42, chat_id=42, route=bad)  # type: ignore[arg-type]


def test_handler_memory_route_defaults_to_telegram(tmp_path: Path) -> None:
    runtime = _Runtime([])
    assert _handler(tmp_path, runtime)._memory_route == "telegram"
    assert _handler(tmp_path, runtime, memory_route="matrix")._memory_route == "matrix"


def test_matrix_ids_are_stable_positive_and_persisted(tmp_path: Path) -> None:
    path = tmp_path / "ids.json"
    ids = MatrixIdMap(path)
    dad = ids.user_id("@dad:example.test")
    room = ids.room_id("!family:example.test")
    assert dad > 0 and room > 0 and dad != room
    assert dad >= 1_000_000_000_000, "never overlaps real Telegram ids or the 0 sentinel"
    assert ids.user_id("@dad:example.test") == dad
    assert MatrixIdMap(path).matrix_id(dad) == "@dad:example.test"
    assert MatrixIdMap(path).user_id("@dad:example.test") == dad
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert ids.matrix_id(12345) is None and ids.matrix_id(True) is None


def test_matrix_direct_rooms_preserve_the_dm_invariant(tmp_path: Path) -> None:
    ids = MatrixIdMap(tmp_path / "ids.json")
    dad = ids.user_id("@dad:example.test")
    dm_chat = ids.chat_id("!dm:example.test", "@dad:example.test", direct=True)
    group_chat = ids.chat_id("!family:example.test", "@dad:example.test", direct=False)
    assert dm_chat == dad and not is_group_conversation(dad, dm_chat)
    assert group_chat != dad and is_group_conversation(dad, group_chat)


def test_matrix_id_map_rejects_bad_ids_and_corrupt_files(tmp_path: Path) -> None:
    ids = MatrixIdMap(None)  # in-memory only
    for bad in ("dad", "@dad", "!room", "@dad:", "", None):
        with pytest.raises(ValueError):
            ids.user_id(bad)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ids.room_id("@dad:example.test")
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json")
    with pytest.raises(ValueError):
        MatrixIdMap(corrupt)
    foreign = tmp_path / "foreign.json"
    foreign.write_text(json.dumps({"ids": {"@a:b": 5}}))  # below the reserved floor
    with pytest.raises(ValueError):
        MatrixIdMap(foreign)
