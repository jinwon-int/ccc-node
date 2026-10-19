"""Steward room tools for the hande frontend (#2380 PR-4).

The native relay bot lets its agent operate rooms through eight *steward*
tools (family-messenger #372 P4 / #378). Validation of names and arguments,
the owner-only rule and the actual MLS/relay operations all live in the bot;
the responder only exposes the tools to the model and forwards each call as a
``tool`` frame, then returns whatever the bot answers in ``tool-result``.

This module is the bridge's copy of the Rust responder's table
(``native-responder/harness.rs`` ``STEWARD_TOOLS`` — same names, same
required/optional arguments, same model-facing text) plus the in-process
Claude Agent SDK MCP server that fronts it. The two read-only tools are
pre-allowed (``mcp_allowed_tools``) exactly as #378 does, so they run without
the per-call approval; everything else goes through the normal approval
round trip first and the bot's owner check always.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import re
from typing import Any, Awaitable, Callable, Mapping

logger = logging.getLogger(__name__)

STEWARD_SERVER = "steward"
ARGUMENT_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")
MAX_RESULT_TEXT_BYTES = 60_000
# Read-only tools (#378): no MLS/relay side effect, so no per-call approval.
STEWARD_READ_ONLY: tuple[str, ...] = ("room_status", "room_members")


@dataclass(frozen=True)
class StewardTool:
    name: str
    required: tuple[str, ...]
    optional: tuple[str, ...]
    description: str

    @property
    def arguments(self) -> tuple[str, ...]:
        return self.required + self.optional


STEWARD_TOOLS: tuple[StewardTool, ...] = (
    StewardTool(
        "room_create",
        ("room",),
        ("mode",),
        "새 E2EE 대화방을 만든다(봇이 방장). mode 는 agent|silent(기본 silent: 봇이 답하지 않음). "
        "릴레이에는 첫 초대 때 생긴다. 오너(owner actor)가 보낸 메시지의 turn 에서만 "
        "실행된다.",
    ),
    StewardTool(
        "room_join",
        ("room",),
        (),
        "다른 기기가 만든 방에 참여 요청(key package 게시). 그 방의 멤버가 봇을 초대해야 들어간다. "
        "오너 turn 전용.",
    ),
    StewardTool(
        "room_invite",
        ("room", "device"),
        (),
        "기기를 방에 초대한다. 상대 기기가 먼저 그 방에 「참여 요청」(key package)을 "
        "해 두어야 한다. device 는 기기 ID(예: jungin-iphone-1b89e3). 오너 turn 전용.",
    ),
    StewardTool(
        "room_remove",
        ("room", "device"),
        (),
        "기기를 방에서 제거한다(되돌릴 수 없음 — 다시 초대해야 한다). 오너 turn 전용.",
    ),
    StewardTool(
        "room_close",
        ("room",),
        (),
        "방을 닫는다(되돌릴 수 없음 — 릴레이가 방을 닫아 모든 멤버에게 「닫힘」이 되고 "
        "다시 열 수 없다. 다시 쓰려면 새 방을 만들어 초대한다). 봇이 답하는 agent 모드 "
        "방은 닫지 않는다(먼저 silent). 오너 turn 전용.",
    ),
    StewardTool(
        "room_members",
        ("room",),
        (),
        "방의 멤버 기기와 epoch·cursor 를 본다(읽기 전용). 오너 turn 전용.",
    ),
    StewardTool(
        "room_mode",
        ("room", "mode"),
        (),
        "방 모드를 바꾼다: agent(봇이 답함, 한 방만) | silent(듣기만). 오너 turn 전용.",
    ),
    StewardTool(
        "room_status",
        (),
        (),
        "봇이 아는 모든 방의 상태(joined·epoch·cursor·mode·pending)를 본다(읽기 전용). "
        "오너 turn 전용.",
    ),
)
_BY_NAME = {tool.name: tool for tool in STEWARD_TOOLS}


def steward_tool(name: str) -> StewardTool | None:
    return _BY_NAME.get(name)


def allowed_tool_names(approve_all: bool = False) -> list[str]:
    """SDK allow-list names for the approval-free read-only tools.

    Empty when every call must be approved (``approve_all``).
    """

    if approve_all:
        return []
    return [f"mcp__{STEWARD_SERVER}__{name}" for name in STEWARD_READ_ONLY if name in _BY_NAME]


def clean_arguments(tool: StewardTool, arguments: Any) -> dict[str, str] | None:
    """The bot's argument shape, checked here first so a bad call never leaves the process.

    Strings matching ``[A-Za-z0-9_-]{1,64}`` only; every required key present;
    no key outside the tool's required + optional set. ``None`` = bad_arguments.
    """

    if arguments is None:
        arguments = {}
    if not isinstance(arguments, Mapping):
        return None
    clean: dict[str, str] = {}
    for key, value in arguments.items():
        if key not in tool.arguments:
            return None
        if not isinstance(value, str) or not ARGUMENT_PATTERN.fullmatch(value):
            return None
        clean[str(key)] = value
    if any(key not in clean for key in tool.required):
        return None
    return clean


def outcome_result(outcome: Mapping[str, Any]) -> dict[str, Any]:
    """MCP tool result for one steward outcome — the Rust responder's shape.

    ``content`` text is the outcome as sorted JSON; ``is_error`` unless
    ``ok`` is exactly ``True``. An oversize outcome is replaced by
    ``result_too_large`` rather than truncated.
    """

    text = json.dumps(outcome, sort_keys=True, ensure_ascii=True, separators=(", ", ": "))
    if len(text.encode("utf-8")) > MAX_RESULT_TEXT_BYTES:
        outcome = {"ok": False, "error": "result_too_large"}
        text = json.dumps(outcome, sort_keys=True, ensure_ascii=True, separators=(", ", ": "))
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if outcome.get("ok") is not True:
        result["is_error"] = True
    return result


def error_result(error: str) -> dict[str, Any]:
    return outcome_result({"ok": False, "error": error})


ToolCall = Callable[[str, Mapping[str, str]], Awaitable[Mapping[str, Any]]]


def build_steward_server(call: ToolCall) -> Any:
    """In-process Claude Agent SDK MCP server fronting the steward table.

    ``call(name, clean_arguments)`` performs the ``tool`` frame round trip
    and returns the bot's outcome (``{"ok": bool, ...}``); this wraps it as
    the MCP result. Returns ``None`` when the SDK is not importable (the
    frontend then runs without steward tools and says so once).
    """

    try:
        from claude_agent_sdk import create_sdk_mcp_server, tool as sdk_tool
    except ImportError:
        logger.warning("hande steward tools disabled: claude_agent_sdk not importable")
        return None

    tools = []
    for spec in STEWARD_TOOLS:

        def make(current: StewardTool) -> Callable[[Mapping[str, Any]], Awaitable[dict[str, Any]]]:
            async def handler(arguments: Mapping[str, Any]) -> dict[str, Any]:
                clean = clean_arguments(current, arguments)
                if clean is None:
                    return error_result("bad_arguments")
                try:
                    outcome = await call(current.name, clean)
                except Exception as exc:  # the bridge never lets a tool crash the turn
                    logger.warning("hande steward %s failed: %s", current.name, type(exc).__name__)
                    return error_result("steward_error")
                return outcome_result(dict(outcome))

            return handler

        schema = {name: str for name in spec.arguments}
        tools.append(sdk_tool(spec.name, spec.description, schema)(make(spec)))
    return create_sdk_mcp_server(name=STEWARD_SERVER, version="1.0.0", tools=tools)
