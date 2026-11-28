"""``send_to_user``: verbatim mid-turn delivery as a model tool (#2942).

The frontend-neutral tool module, the hande sink that reports what the port
did with the message, and the bot wiring that declares the tool only from a
session's first turn.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from telegram_bot.contracts.transport import TurnResult
from telegram_bot.core import send_to_user
from telegram_bot.core.hande import steward
from telegram_bot.core.hande.bot import HandeBot
from telegram_bot.core.hande.transport import HandeTransport, _ActiveTurn, _Client, _HandeSink
from test_hande_transport import FakeBot, FakeRunner, _config, _connected, _serve, _stop
from test_matrix_bot import FakeProjectChat, FakeSessionManager

pytestmark = pytest.mark.anyio

# A body of the kind that went missing: a heading, a fenced block, Korean text.
BODY = "**(4/11) 안내문**\n\n```\n현황 기준 항목 4건입니다.\n\t1. 첫째 항목 — 확인 뒤 닫기\n```"


@pytest.fixture
def tmp_path(short_tmp_path):
    """Unix-socket suite: keep socket paths under the sun_path limit (#2598)."""
    return short_tmp_path


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _outcome(result: dict[str, Any]) -> dict[str, Any]:
    return json.loads(result["content"][0]["text"])


# -- tool module --------------------------------------------------------------------


def test_names() -> None:
    assert send_to_user.qualified() == "mcp__bridge__send_to_user"
    assert send_to_user.allowed_tool_names() == ["mcp__bridge__send_to_user"]


def test_clean_message_keeps_the_body_byte_for_byte() -> None:
    message, error = send_to_user.clean_message({"message": BODY}, max_bytes=8192)
    assert error is None
    assert message == BODY
    assert message.encode("utf-8") == BODY.encode("utf-8")
    # Leading/trailing whitespace and blank lines are the author's, not ours.
    padded = "\n  first line\n\nlast line  \n"
    assert send_to_user.clean_message({"message": padded}, max_bytes=8192) == (padded, None)


def test_clean_message_drops_only_control_and_ansi_sequences() -> None:
    message, error = send_to_user.clean_message(
        {"message": "a\x1b[31mred\x1b[0m\x00b\x07\tc\r\nd"}, max_bytes=8192
    )
    assert error is None and message == "aredb\tc\r\nd"


@pytest.mark.parametrize(
    "arguments",
    [None, "text", {}, {"message": 3}, {"message": "x", "room": "agent"}, {"text": "x"}],
)
def test_clean_message_rejects_other_shapes(arguments: Any) -> None:
    assert send_to_user.clean_message(arguments, max_bytes=8192) == (
        None,
        {"ok": False, "error": "bad_arguments"},
    )


def test_clean_message_rejects_blank_and_oversize_without_truncating() -> None:
    assert send_to_user.clean_message({"message": " \n\t"}, max_bytes=8192) == (
        None,
        {"ok": False, "error": "empty_message"},
    )
    # The bound is bytes, not characters: 3000 Hangul syllables are 9000 bytes.
    korean = "가" * 3000
    assert send_to_user.clean_message({"message": korean}, max_bytes=8192) == (
        None,
        {"ok": False, "error": "too_long", "bytes": 9000, "max_bytes": 8192},
    )
    assert send_to_user.clean_message({"message": "가" * 2730}, max_bytes=8192)[1] is None


def test_outcome_result_shape() -> None:
    good = send_to_user.outcome_result({"ok": True, "bytes": 12})
    assert good == {"content": [{"type": "text", "text": '{"bytes": 12, "ok": true}'}]}
    bad = send_to_user.outcome_result({"ok": False, "error": "interim_limit"})
    assert bad["is_error"] is True and _outcome(bad)["error"] == "interim_limit"
    # "ok" must be exactly True for a success.
    assert send_to_user.outcome_result({"ok": 1})["is_error"] is True


async def test_handler_sends_the_cleaned_message_and_reports_the_outcome() -> None:
    sent: list[str] = []

    async def send(message: str) -> dict[str, Any]:
        sent.append(message)
        return {"ok": True, "bytes": len(message.encode("utf-8"))}

    handler = send_to_user.make_handler(send, lambda: 8192)
    result = await handler({"message": BODY})
    assert "is_error" not in result
    assert _outcome(result) == {"ok": True, "bytes": len(BODY.encode("utf-8"))}
    assert sent == [BODY]
    # A refused call never reaches the frontend.
    refused = await handler({"message": "x" * 9000})
    assert refused["is_error"] is True and _outcome(refused)["error"] == "too_long"
    assert sent == [BODY]


async def test_handler_turns_frontend_failures_into_send_failed() -> None:
    async def boom(message: str) -> dict[str, Any]:
        raise RuntimeError("socket gone")

    async def odd(message: str) -> Any:
        return "sent"

    def broken_limit() -> int:
        raise ValueError("no transport")

    for send in (boom, odd):
        result = await send_to_user.make_handler(send, lambda: 8192)({"message": "hello"})
        assert result["is_error"] is True and _outcome(result) == {"ok": False, "error": "send_failed"}

    # An unreadable limit falls back to the default bound instead of failing the call.
    async def ok(message: str) -> dict[str, Any]:
        return {"ok": True, "bytes": 5}

    assert _outcome(await send_to_user.make_handler(ok, broken_limit)({"message": "hello"})) == {
        "ok": True,
        "bytes": 5,
    }


async def test_server_carries_one_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_tool(name: str, description: str, schema: Any):
        def deco(fn):
            captured[name] = (description, schema, fn)
            return fn

        return deco

    def fake_server(*, name: str, version: str, tools: list) -> Any:
        return SimpleNamespace(name=name, version=version, tools=tools)

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "tool", fake_tool)
    monkeypatch.setattr(claude_agent_sdk, "create_sdk_mcp_server", fake_server)

    async def send(message: str) -> dict[str, Any]:
        return {"ok": True, "bytes": len(message.encode("utf-8"))}

    server = send_to_user.build_send_to_user_server(send, max_bytes=lambda: 8192)
    assert server.name == "bridge" and len(server.tools) == 1
    description, schema, handler = captured["send_to_user"]
    assert schema == {"message": str}
    assert "exactly as written" in description and "never summarized" in description
    assert _outcome(await handler({"message": "hi"})) == {"ok": True, "bytes": 2}


def test_server_is_none_without_the_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)

    async def send(message: str) -> dict[str, Any]:
        return {"ok": True}

    assert send_to_user.build_send_to_user_server(send, max_bytes=lambda: 8192) is None


def test_merge_request_extras_keeps_other_servers() -> None:
    extras: dict[str, Any] = {
        "mcp_servers": {"steward": "S"},
        "mcp_allowed_tools": steward.all_tool_names(),
    }
    send_to_user.merge_request_extras(extras, "B")
    assert extras["mcp_servers"] == {"steward": "S", "bridge": "B"}
    assert extras["mcp_allowed_tools"] == [*steward.all_tool_names(), "mcp__bridge__send_to_user"]
    send_to_user.merge_request_extras(extras, "B")  # idempotent
    assert extras["mcp_allowed_tools"].count("mcp__bridge__send_to_user") == 1
    alone: dict[str, Any] = {}
    send_to_user.merge_request_extras(alone, "B")
    assert alone == {"mcp_servers": {"bridge": "B"}, "mcp_allowed_tools": ["mcp__bridge__send_to_user"]}


def test_offered_only_from_a_sessions_first_turn() -> None:
    assert send_to_user.offered_for_session({}, None) is True
    assert send_to_user.offered_for_session({"send_to_user_session": "s-old"}, None) is True
    assert send_to_user.offered_for_session({"send_to_user_session": "s-1"}, "s-1") is True
    # A session already under way without the tool never gains it.
    assert send_to_user.offered_for_session({}, "s-1") is False
    assert send_to_user.offered_for_session({"send_to_user_session": "s-0"}, "s-1") is False


# -- hande sink -----------------------------------------------------------------------


async def test_hande_sink_posts_the_message_verbatim_and_reports_bytes(tmp_path: Path) -> None:
    served = await _serve(tmp_path, interim_frames=True)
    outcomes: list[dict[str, Any]] = []

    async def turn(job: Any, sink: Any) -> TurnResult:
        outcomes.append(await sink.send_to_user(BODY))
        return TurnResult(text="done", session_id="s")

    served.runner.behaviour = turn
    try:
        bot = await FakeBot.connect(served.path)
        await _connected(served)
        await bot.send(bot.turn("t-1", "go"))
        assert await bot.recv_kind("interim") == {"type": "interim", "turn_id": "t-1", "text": BODY}
        assert (await bot.recv_kind("result"))["text"] == "done"
        assert outcomes == [{"ok": True, "bytes": len(BODY.encode("utf-8")), "interims_left": 39}]
        assert served.transport.health_signals()["notices_dropped"] == 0
        await bot.close()
    finally:
        await _stop(served)


async def test_hande_sink_delivers_ten_bodies_at_their_exact_size(tmp_path: Path) -> None:
    # The #2942 measurement: ten 2-3 KB bodies in one turn, each arriving whole.
    bodies = [f"({index}/10) " + ("본문 줄 " * 300) + f"\n```\nitem {index}\n```" for index in range(1, 11)]
    assert all(2000 < len(body.encode("utf-8")) < 4000 for body in bodies)
    served = await _serve(tmp_path, interim_frames=True)
    outcomes: list[dict[str, Any]] = []

    async def turn(job: Any, sink: Any) -> TurnResult:
        for body in bodies:
            outcomes.append(await sink.send_to_user(body))
        return TurnResult(text="done", session_id="s")

    served.runner.behaviour = turn
    try:
        bot = await FakeBot.connect(served.path)
        await _connected(served)
        await bot.send(bot.turn("t-2", "go"))
        received = [(await bot.recv_kind("interim"))["text"] for _ in bodies]
        assert received == bodies
        assert [o["bytes"] for o in outcomes] == [len(body.encode("utf-8")) for body in bodies]
        assert all(o["ok"] is True for o in outcomes)
        assert (await bot.recv_kind("result"))["text"] == "done"
        await bot.close()
    finally:
        await _stop(served)


async def test_hande_sink_reports_instead_of_cutting_or_dropping(tmp_path: Path) -> None:
    served = await _serve(tmp_path, interim_frames=True, bot_interim_max=2, bot_reply_max_bytes=4096)
    outcomes: list[dict[str, Any]] = []

    async def turn(job: Any, sink: Any) -> TurnResult:
        outcomes.append(await sink.send_to_user("x" * 4097))  # over one bubble
        outcomes.append(await sink.send_to_user("first"))
        outcomes.append(await sink.send_to_user("second"))
        outcomes.append(await sink.send_to_user("third"))  # the bot would drop it
        return TurnResult(text="done", session_id="s")

    served.runner.behaviour = turn
    try:
        bot = await FakeBot.connect(served.path)
        await _connected(served)
        await bot.send(bot.turn("t-3", "go"))
        texts = []
        while True:
            frame = await bot.recv()
            if frame["type"] == "interim":
                texts.append(frame["text"])
            if frame["type"] == "result":
                break
        assert texts == ["first", "second"]
        assert outcomes == [
            {"ok": False, "error": "too_long", "bytes": 4097, "max_bytes": 4096},
            {"ok": True, "bytes": 5, "interims_left": 1},
            {"ok": True, "bytes": 6, "interims_left": 0},
            {"ok": False, "error": "interim_limit", "interims_sent": 2, "interim_max": 2},
        ]
        await bot.close()
    finally:
        await _stop(served)


async def test_hande_sink_says_so_when_interim_frames_are_off(tmp_path: Path) -> None:
    served = await _serve(tmp_path, interim_frames=False)
    outcomes: list[dict[str, Any]] = []

    async def turn(job: Any, sink: Any) -> TurnResult:
        outcomes.append(await sink.send_to_user("hello"))
        return TurnResult(text="done", session_id="s")

    served.runner.behaviour = turn
    try:
        bot = await FakeBot.connect(served.path)
        await _connected(served)
        await bot.send(bot.turn("t-4", "go"))
        seen = set()
        while True:
            frame = await bot.recv()
            seen.add(frame["type"])
            if frame["type"] == "result":
                break
        assert "interim" not in seen
        assert outcomes == [{"ok": False, "error": "interim_frames_off"}]
        assert served.transport.health_signals()["notices_dropped"] == 1
        await bot.close()
    finally:
        await _stop(served)


async def test_hande_sink_is_inert_once_the_turn_is_over(tmp_path: Path) -> None:
    transport = HandeTransport(_config(interim_frames=True), FakeRunner(), socket_path=tmp_path / "s.sock")
    client = _Client(reader=None, writer=None)  # type: ignore[arg-type]
    client.closed = True
    sink = _HandeSink(transport, client, _ActiveTurn("t-x", {"event_id": "t-x"}))
    assert await sink.send_to_user("late") == {"ok": False, "error": "no_active_turn"}


# -- bot wiring -------------------------------------------------------------------------


def _bot(tmp_path: Path, **config_overrides: Any) -> HandeBot:
    config = {
        "account": "@bridgenode:hande",
        "owner": "@owner:hande",
        "rooms": ["agent"],
        "socket_path": "/run/ccc-hande/responder.sock",
        **config_overrides,
    }
    path = tmp_path / "hande.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    os.chmod(path, 0o600)
    settings = SimpleNamespace(
        agent_provider="claude",
        project_root=tmp_path,
        execution_profile="owner-operator",
        bash_policy="approve-each",
        allowed_user_ids=[7],
        require_allowlist=True,
        claude_settings_path=tmp_path / "claude" / "settings.json",
        bot_data_dir=tmp_path / "data",
        hande_config_path=path,
        hande_socket_path=None,
        telegram_session_scope="per-user-chat",
        bridge_memory_mode="off",
        auto_new_session_after_hours=None,
        matrix_startup_banner=False,
        channel="hande",
    )
    return HandeBot(
        settings, project_chat=FakeProjectChat(), session_manager=FakeSessionManager("claude")
    )


@pytest.fixture
def fake_server(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    built: list[Any] = []

    def build(send: Any, *, max_bytes: Any) -> Any:
        built.append((send, max_bytes))
        return SimpleNamespace(name="bridge")

    monkeypatch.setattr(send_to_user, "build_send_to_user_server", build)
    monkeypatch.setattr(
        "telegram_bot.core.hande.bot.build_steward_server", lambda call: SimpleNamespace(name="steward")
    )
    return built


def test_tool_is_off_by_default(tmp_path: Path, fake_server: list[Any]) -> None:
    bot = _bot(tmp_path)
    extra: dict[str, Any] = {}
    assert bot._offer_send_to_user(extra, {}, None) is False
    assert extra == {} and fake_server == []


def test_tool_joins_the_steward_server_on_a_fresh_session(
    tmp_path: Path, fake_server: list[Any]
) -> None:
    bot = _bot(tmp_path, send_to_user_tool=True)
    extra = bot._turn_request_extras()
    assert bot._offer_send_to_user(extra, {}, None) is True
    assert set(extra["mcp_servers"]) == {"steward", "bridge"}
    assert extra["mcp_allowed_tools"] == [*steward.all_tool_names(), "mcp__bridge__send_to_user"]
    # Built once, reused on later turns.
    again: dict[str, Any] = {}
    assert bot._offer_send_to_user(again, {"send_to_user_session": "s-1"}, "s-1") is True
    assert again["mcp_servers"]["bridge"] is extra["mcp_servers"]["bridge"]
    assert len(fake_server) == 1


def test_tool_is_never_added_to_a_session_under_way(tmp_path: Path, fake_server: list[Any]) -> None:
    bot = _bot(tmp_path, send_to_user_tool=True)
    for session in ({}, {"send_to_user_session": "s-other"}):
        extra: dict[str, Any] = {}
        assert bot._offer_send_to_user(extra, session, "s-1") is False
        assert extra == {}
    assert fake_server == []


def test_tool_needs_the_claude_provider_and_the_sdk(
    tmp_path: Path, fake_server: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    codex = _bot(tmp_path, send_to_user_tool=True)
    codex._settings.agent_provider = "codex"
    assert codex._offer_send_to_user({}, {}, None) is False
    monkeypatch.setattr(send_to_user, "build_send_to_user_server", lambda send, *, max_bytes: None)
    missing_sdk = _bot(tmp_path, send_to_user_tool=True)
    extra: dict[str, Any] = {}
    assert missing_sdk._offer_send_to_user(extra, {}, None) is False and extra == {}


def test_config_rejects_a_non_boolean_knob(tmp_path: Path) -> None:
    from telegram_bot.core.hande.state import HandeConfigError

    with pytest.raises(HandeConfigError, match="send_to_user_tool"):
        _bot(tmp_path, send_to_user_tool="yes").load_config()


def test_max_bytes_follows_the_transport(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    assert bot._send_to_user_max_bytes() == 8192  # no transport yet
    bot._transport = SimpleNamespace(bot_reply_max_bytes=4096)
    assert bot._send_to_user_max_bytes() == 4096
    bot._transport = SimpleNamespace(bot_reply_max_bytes=65536)
    assert bot._send_to_user_max_bytes() == 8192  # never above one interim frame


async def test_send_goes_to_the_turn_being_served(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot = _bot(tmp_path)
    assert await bot._send_to_user("hello") == {"ok": False, "error": "no_active_turn"}

    class ReportingSink:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send_to_user(self, text: str) -> dict[str, Any]:
            self.sent.append(text)
            return {"ok": True, "bytes": len(text.encode("utf-8")), "interims_left": 3}

    reporting = ReportingSink()
    bot._active_sink = reporting
    assert await bot._send_to_user(BODY) == {
        "ok": True,
        "bytes": len(BODY.encode("utf-8")),
        "interims_left": 3,
    }
    assert reporting.sent == [BODY]

    # A sink without its own method takes the interim path of the turn's room.
    class PlainSink:
        def __init__(self) -> None:
            self.interims: list[str] = []

        async def interim(self, text: str) -> None:
            self.interims.append(text)

    rooms: list[str] = []

    async def no_formatted(room_id: str, text: str) -> bool:
        rooms.append(room_id)
        return False

    monkeypatch.setattr(bot, "_send_formatted", no_formatted)
    plain = PlainSink()
    bot._active_sink = plain
    bot._active_room_id = "agent"
    assert await bot._send_to_user("가나다") == {"ok": True, "bytes": 9, "confirmed": False}
    assert plain.interims == ["가나다"] and rooms == ["agent"]


class _TurnSink:
    async def typing(self) -> None:
        return None

    async def interim(self, text: str) -> None:
        return None

    async def status(self, text: str | None) -> None:
        return None


async def _run(bot: HandeBot, body: str = "hi") -> dict[str, Any]:
    owner = bot._owner_int()  # owner-operator refuses every other sender
    assert owner is not None
    await bot._run_message(body, user_id=owner, chat_id=owner, room_id="agent", sink=_TurnSink())
    return bot._project_chat.calls[-1]


async def test_a_new_session_gets_the_tool_and_keeps_it(
    tmp_path: Path, fake_server: list[Any]
) -> None:
    bot = _bot(tmp_path, send_to_user_tool=True)
    sessions = bot._session_manager
    chat = bot._project_chat

    first = await _run(bot)
    assert set(first["mcp_servers"]) == {"steward", "bridge"}
    assert "mcp__bridge__send_to_user" in first["mcp_allowed_tools"]
    key = next(iter(sessions.rows))
    # The provider session that carries the tool is remembered ...
    assert sessions.rows[key]["send_to_user_session"] == "s-new"
    # ... so its next turn (a resume of s-new) declares the tool again.
    second = await _run(bot)
    assert second["session_id"] == "s-new" and "bridge" in second["mcp_servers"]

    # A different provider session in the same room (e.g. /resume of an older
    # one) does not gain the tool mid-session.
    sessions.rows[key]["session_id"] = "s-older"
    chat.response = type(chat.response)(content="answer", session_id="s-older")
    third = await _run(bot)
    assert third["session_id"] == "s-older"
    assert set(third["mcp_servers"]) == {"steward"}
    assert sessions.rows[key]["send_to_user_session"] == "s-new"


async def test_an_existing_session_waits_for_the_next_new_one(
    tmp_path: Path, fake_server: list[Any]
) -> None:
    bot = _bot(tmp_path, send_to_user_tool=True)
    sessions = bot._session_manager
    await _run(bot)
    key = next(iter(sessions.rows))
    # A session that started before the knob was switched on: no marker.
    sessions.rows[key].pop("send_to_user_session")
    under_way = await _run(bot)
    assert under_way["session_id"] == "s-new" and set(under_way["mcp_servers"]) == {"steward"}
    assert "send_to_user_session" not in sessions.rows[key]
    # /new: the next turn starts a fresh provider session and declares the tool.
    sessions.rows[key]["new_session"] = True
    fresh = await _run(bot)
    assert fresh["new_session"] is True and "bridge" in fresh["mcp_servers"]
    assert sessions.rows[key]["send_to_user_session"] == "s-new"


async def test_a_failed_turn_leaves_no_marker(tmp_path: Path, fake_server: list[Any]) -> None:
    bot = _bot(tmp_path, send_to_user_tool=True)
    chat = bot._project_chat
    chat.response = type(chat.response)(content="boom", success=False, session_id="s-bad")
    call = await _run(bot)
    assert "bridge" in call["mcp_servers"]
    key = next(iter(bot._session_manager.rows))
    assert "send_to_user_session" not in bot._session_manager.rows[key]


async def test_the_tool_is_absent_when_the_knob_is_off(tmp_path: Path, fake_server: list[Any]) -> None:
    bot = _bot(tmp_path)
    call = await _run(bot)
    assert set(call["mcp_servers"]) == {"steward"}
    key = next(iter(bot._session_manager.rows))
    assert "send_to_user_session" not in bot._session_manager.rows[key]
