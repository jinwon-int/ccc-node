"""Steward room tools over the hande port (#2380 PR-4).

Table parity with the Rust responder, argument cleaning, MCP result shape,
the ``tool``/``tool-result`` round trip through ``HandeTransport``, and the
``HandeBot`` request extras that put the server on every turn.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from telegram_bot.contracts.transport import TurnResult
from telegram_bot.core.hande import steward
from telegram_bot.core.hande.bot import HandeBot
from telegram_bot.core.hande.transport import HandeTransport
from test_hande_transport import FakeBot, FakeRunner, _config, _connected, _serve, _stop

pytestmark = pytest.mark.anyio


@pytest.fixture
def tmp_path(short_tmp_path):
    """Unix-socket suite: keep socket paths under the sun_path limit (#2598)."""
    return short_tmp_path


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# -- table -----------------------------------------------------------------------


def test_table_matches_the_rust_responder() -> None:
    names = [tool.name for tool in steward.STEWARD_TOOLS]
    assert names == [
        "room_create",
        "room_join",
        "room_invite",
        "room_remove",
        "room_close",
        "room_members",
        "room_mode",
        "room_status",
    ]
    assert steward.steward_tool("room_create").arguments == ("room", "mode")
    assert steward.steward_tool("room_invite").required == ("room", "device")
    assert steward.steward_tool("room_status").arguments == ()
    assert steward.allowed_tool_names() == [
        "mcp__steward__room_status",
        "mcp__steward__room_members",
    ]
    assert steward.allowed_tool_names(approve_all=True) == []
    assert (
        len(steward.all_tool_names()) == 8
        and steward.all_tool_names()[0] == "mcp__steward__room_create"
    )
    assert steward.needs_approval("room_status") is False
    assert steward.needs_approval("room_members") is False
    assert steward.needs_approval("room_mode") is True
    assert steward.needs_approval("room_status", approve_all=True) is True


@pytest.mark.parametrize(
    "name, arguments, expected",
    [
        ("room_create", {"room": "r3"}, {"room": "r3"}),
        ("room_create", {"room": "r3", "mode": "agent"}, {"room": "r3", "mode": "agent"}),
        ("room_create", {"room": "r3", "device": "x"}, None),  # not an argument of this tool
        ("room_invite", {"room": "family"}, None),  # device required
        ("room_invite", {"room": "family", "device": "a b"}, None),  # shape
        ("room_invite", {"room": "family", "device": 7}, None),  # strings only
        ("room_status", {}, {}),
        ("room_status", None, {}),
        ("room_status", {"room": "r"}, None),  # no extra keys
        ("room_close", ["r"], None),
    ],
)
def test_clean_arguments(name: str, arguments: Any, expected: Any) -> None:
    assert steward.clean_arguments(steward.steward_tool(name), arguments) == expected


def test_outcome_result_shape() -> None:
    ok = steward.outcome_result({"ok": True, "room": "r", "seq": 9})
    assert ok == {"content": [{"type": "text", "text": '{"ok": true, "room": "r", "seq": 9}'}]}
    err = steward.outcome_result({"ok": False, "error": "owner_only"})
    assert err["is_error"] is True
    assert json.loads(err["content"][0]["text"]) == {"error": "owner_only", "ok": False}
    huge = steward.outcome_result({"ok": True, "blob": "x" * 70_000})
    assert huge["is_error"] is True and "result_too_large" in huge["content"][0]["text"]
    assert steward.error_result("bad_arguments")["is_error"] is True


async def test_server_handlers_clean_then_forward(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, str]]] = []

    async def call(name: str, arguments: Any) -> dict[str, Any]:
        calls.append((name, dict(arguments)))
        return {"ok": True, "op": name}

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
    server = steward.build_steward_server(call)
    assert server.name == "steward" and len(server.tools) == 8
    _, schema, invite = captured["room_invite"]
    assert schema == {"room": str, "device": str}
    good = await invite({"room": "family", "device": "kid-iphone-1b89e3"})
    assert good == {"content": [{"type": "text", "text": '{"ok": true, "op": "room_invite"}'}]}
    bad = await invite({"room": "family"})
    assert bad["is_error"] is True and "bad_arguments" in bad["content"][0]["text"]
    assert calls == [("room_invite", {"room": "family", "device": "kid-iphone-1b89e3"})]


# -- transport round trip ----------------------------------------------------------


async def test_tool_frame_round_trip_ok_error_timeout_and_busy(tmp_path: Path) -> None:
    served = await _serve(tmp_path, tool_timeout_s=1)
    outcomes: list[Any] = []

    async def uses_tools(job: Any, sink: Any) -> TurnResult:
        outcomes.append(await sink.tool("room_status", {}))
        outcomes.append(await sink.tool("room_invite", {"room": "family", "device": "kid-1"}))
        first = asyncio.create_task(sink.tool("room_members", {"room": "family"}))
        await asyncio.sleep(0.05)
        outcomes.append(await sink.tool("room_status", {}))  # overlapping → busy
        outcomes.append(await first)  # times out (no answer)
        return TurnResult(text="done", session_id="s")

    served.runner.behaviour = uses_tools
    try:
        bot = await FakeBot.connect(served.path)
        await _connected(served)
        await bot.send(bot.turn("t-20", "status please"))
        ask = await bot.recv_kind("tool")
        assert ask["turn_id"] == "t-20" and ask["name"] == "room_status" and ask["arguments"] == {}
        # Wrong tool_id: not accepted, nothing settles.
        await bot.send({"type": "tool-result", "turn_id": "t-20", "tool_id": "bogus", "ok": True})
        assert await bot.recv() == {"type": "control", "turn_id": "t-20", "accepted": False}
        await bot.send(
            {
                "type": "tool-result",
                "turn_id": "t-20",
                "tool_id": ask["tool_id"],
                "ok": True,
                "result": {"op": "status", "rooms": [{"room": "family-acc", "joined": True}]},
            }
        )
        assert await bot.recv() == {"type": "control", "turn_id": "t-20", "accepted": True}
        ask2 = await bot.recv_kind("tool")
        assert ask2["name"] == "room_invite" and ask2["arguments"] == {
            "room": "family",
            "device": "kid-1",
        }
        await bot.send(
            {
                "type": "tool-result",
                "turn_id": "t-20",
                "tool_id": ask2["tool_id"],
                "ok": False,
                "error": "owner_only",
            }
        )
        await bot.recv_kind("control")
        ask3 = await bot.recv_kind("tool")
        assert ask3["name"] == "room_members"
        result = await bot.recv_kind("result", timeout=5)
        assert result["status"] == "complete"
        assert outcomes[0] == {
            "ok": True,
            "op": "status",
            "rooms": [{"room": "family-acc", "joined": True}],
        }
        assert outcomes[1] == {"ok": False, "error": "owner_only"}
        assert outcomes[2] == {"ok": False, "error": "busy"}
        assert outcomes[3] == {"ok": False, "error": "steward_timeout"}
        await bot.close()
    finally:
        await _stop(served)


async def test_cancel_settles_a_pending_tool_as_cancelled(tmp_path: Path) -> None:
    served = await _serve(tmp_path)
    seen: list[Any] = []

    async def waits_on_tool(job: Any, sink: Any) -> TurnResult:
        seen.append(await sink.tool("room_create", {"room": "r9"}))
        await asyncio.sleep(10)
        return TurnResult(text="never", session_id="s")

    served.runner.behaviour = waits_on_tool
    try:
        bot = await FakeBot.connect(served.path)
        await _connected(served)
        await bot.send(bot.turn("t-21", "make a room"))
        await bot.recv_kind("tool")
        await bot.send({"type": "cancel", "turn_id": "t-21"})
        await bot.recv_kind("control")
        result = await bot.recv_kind("result")
        assert result["status"] == "uncertain" and result["reason"] == "cancelled"
        assert seen == [{"ok": False, "error": "cancelled"}]
        await bot.close()
    finally:
        await _stop(served)


async def test_tool_outside_a_live_turn_is_no_active_turn(tmp_path: Path) -> None:
    transport = HandeTransport(_config(), FakeRunner(), socket_path=tmp_path / "s.sock")
    from telegram_bot.core.hande.transport import _ActiveTurn, _Client, _HandeSink

    client = _Client(
        asyncio.StreamReader(), SimpleNamespace(write=lambda d: None, close=lambda: None)
    )
    client.closed = True
    sink = _HandeSink(transport, client, _ActiveTurn("t-x", {"event_id": "t-x"}))
    assert await sink.tool("room_status", {}) == {"ok": False, "error": "no_active_turn"}


# -- bot wiring --------------------------------------------------------------------


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
    return HandeBot(settings, project_chat=object(), session_manager=object())


def test_turn_request_extras_carry_the_steward_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot = _bot(tmp_path)
    built: list[Any] = []
    monkeypatch.setattr(
        "telegram_bot.core.hande.bot.build_steward_server",
        lambda call: built.append(call) or SimpleNamespace(name="steward"),
    )
    extras = bot._turn_request_extras()
    assert set(extras) == {"mcp_servers", "mcp_allowed_tools"}
    assert extras["mcp_servers"]["steward"].name == "steward"
    # PR-5: all eight pre-allowed at the SDK level; the bridge asks the owner itself.
    assert extras["mcp_allowed_tools"] == steward.all_tool_names()
    # Built once, reused on the next turn.
    assert bot._turn_request_extras()["mcp_servers"]["steward"] is extras["mcp_servers"]["steward"]
    assert len(built) == 1


def test_turn_request_extras_respect_config_and_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "telegram_bot.core.hande.bot.build_steward_server", lambda call: SimpleNamespace(name="s")
    )
    off = _bot(tmp_path, steward_tools=False)
    assert off._turn_request_extras() == {}
    approve_all = _bot(tmp_path, steward_tools_approve_all=True)
    assert approve_all._turn_request_extras()["mcp_allowed_tools"] == steward.all_tool_names()
    assert approve_all._steward_approve_all() is True
    codex = _bot(tmp_path)
    codex._settings.agent_provider = "codex"
    assert codex._turn_request_extras() == {}
    missing_sdk = _bot(tmp_path)
    monkeypatch.setattr("telegram_bot.core.hande.bot.build_steward_server", lambda call: None)
    assert missing_sdk._turn_request_extras() == {}


class _ApprovingSink:
    """A sink that records approval prompts and answers them as scripted."""

    def __init__(self, answers: list[str]) -> None:
        self.answers = answers
        self.prompts: list[str] = []
        self.tools: list[tuple[str, dict[str, str]]] = []

    async def approval_outcome(self, text: str) -> str:
        self.prompts.append(text)
        return self.answers.pop(0)

    async def tool(self, name: str, arguments: Any) -> dict[str, Any]:
        self.tools.append((name, dict(arguments)))
        return {"ok": True, "op": name}


async def test_steward_call_read_only_goes_straight_through(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    assert await bot._steward_call("room_status", {}) == {"ok": False, "error": "no_active_turn"}
    sink = _ApprovingSink([])
    bot._active_sink = sink
    assert await bot._steward_call("room_members", {"room": "r"}) == {
        "ok": True,
        "op": "room_members",
    }
    assert sink.prompts == [] and sink.tools == [("room_members", {"room": "r"})]


async def test_steward_call_asks_the_owner_for_write_tools(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    sink = _ApprovingSink(["allow", "deny", "timeout"])
    bot._active_sink = sink
    bot._steward_identity = (7, 7)
    ok = await bot._steward_call("room_mode", {"room": "family-acc", "mode": "silent"})
    assert ok == {"ok": True, "op": "room_mode"}
    denied = await bot._steward_call("room_remove", {"room": "family-acc", "device": "kid-1"})
    assert denied == {"ok": False, "error": "approval_deny"}
    timed_out = await bot._steward_call("room_close", {"room": "old"})
    assert timed_out == {"ok": False, "error": "approval_timeout"}
    # Only the approved call reached the bot; prompts carry the redacted snapshot.
    assert sink.tools == [("room_mode", {"room": "family-acc", "mode": "silent"})]
    assert len(sink.prompts) == 3
    assert "도구: room_mode" in sink.prompts[0] and "room=family-acc mode=silent" in sink.prompts[0]
    assert "인수: room=old" in sink.prompts[2]
    # Audit ledger: asked/answered pairs for all three.
    ledger_dir = Path(bot._settings.bot_data_dir) / "approval-audit"
    rows = []
    for f in sorted(ledger_dir.glob("*.jsonl")):
        rows += [json.loads(line) for line in f.read_text().splitlines() if line.strip()]
    assert [r["event"] for r in rows] == ["asked", "answered"] * 3
    assert [r.get("decision") for r in rows if r["event"] == "answered"] == [
        "allow",
        "deny",
        "timeout",
    ]
    assert all(r["action"] == "tool_use" for r in rows)


async def test_steward_approve_all_asks_for_read_only_too(tmp_path: Path) -> None:
    bot = _bot(tmp_path, steward_tools_approve_all=True)
    sink = _ApprovingSink(["allow"])
    bot._active_sink = sink
    assert (await bot._steward_call("room_status", {}))["ok"] is True
    assert len(sink.prompts) == 1


async def test_run_turn_tracks_identity_for_the_audit(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    seen: dict[str, Any] = {}

    async def fake_super(job: Any, *, sink: Any, session_id: Any, room_kind: str) -> Any:
        seen["identity"] = bot._steward_identity
        return TurnResult(text="ok", session_id=None)

    import telegram_bot.core.matrix.bot as matrix_bot_module

    original = matrix_bot_module.MatrixBot.run_turn
    matrix_bot_module.MatrixBot.run_turn = lambda self, job, *, sink, session_id, room_kind: (
        fake_super(job, sink=sink, session_id=session_id, room_kind=room_kind)
    )
    try:
        job = {"event_id": "t-1", "room_id": "agent", "sender": "@owner:hande", "body": "hi"}
        await bot.run_turn(job, sink=object(), session_id=None, room_kind="direct")
    finally:
        matrix_bot_module.MatrixBot.run_turn = original
    assert seen["identity"] is not None and bot._steward_identity is None


# -- process_message passthrough -----------------------------------------------------


async def test_process_message_binds_caller_mcp_fields_to_the_session_request(
    tmp_path: Path,
) -> None:
    from test_project_chat_codex import FakeRuntime, _handler

    runtime = FakeRuntime()
    handler = _handler(tmp_path, runtime)
    # The SDK's create_sdk_mcp_server returns a config mapping; SessionRequest
    # validates "non-empty mapping", so a bare object would be refused.
    server = {"type": "sdk", "name": "steward", "instance": object()}
    await handler.process_message(
        "status please",
        user_id=7,
        chat_id=7,
        new_session=True,
        mcp_servers={"steward": server},
        mcp_allowed_tools=["mcp__steward__room_status"],
    )
    request = runtime.requests[0]
    assert dict(request.mcp_servers) == {"steward": server}
    assert tuple(request.mcp_allowed_tools) == ("mcp__steward__room_status",)
    # Without the fields the request is unchanged (no empty mapping sneaks in).
    await handler.process_message("plain", user_id=8, chat_id=8, new_session=True)
    assert runtime.requests[1].mcp_servers is None
    assert runtime.requests[1].mcp_allowed_tools is None
