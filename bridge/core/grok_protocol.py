"""Bounded Grok Bot gateway validation, qualified against host 5c534e9.

This is the existing Bot protocol, not the xAI model API. Acceptance is not
completion. A caller must durably retain the nonce, prompt and pre-send
baseline, and must not resend an uncertain operation under a fresh nonce.
No response/error body is ever included in a ProtocolError.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import uuid
from typing import Any

HOST_VERSION = "5c534e9"
MAX_WIRE = 1024 * 1024
MAX_PROMPT = 32768
MAX_ENTRIES = 64
MAX_REPLY = 65536


class ProtocolError(ValueError):
    """Categorical, body-free failure; never an instruction to resend."""


def _text(value: Any, limit: int, code: str) -> str:
    if not isinstance(value, str) or not value or "\0" in value:
        raise ProtocolError(code)
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        raise ProtocolError(code) from None
    if size > limit:
        raise ProtocolError(code)
    return value


def identifier(value: Any) -> str:
    value = _text(value, 256, "invalid_identifier")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ProtocolError("invalid_identifier")
    return value


def _uuid(value: Any) -> str:
    value = identifier(value)
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError
    except ValueError:
        raise ProtocolError("invalid_uuid") from None
    return value


def decode_wire(raw: bytes) -> Any:
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_WIRE:
        raise ProtocolError("invalid_wire_size")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ProtocolError("duplicate_json_key")
            result[key] = value
        return result

    def bad_constant(_: str) -> None:
        raise ProtocolError("invalid_json")

    def bounded_integer(value: str) -> int:
        if len(value.lstrip("-")) > 20:
            raise ProtocolError("json_number_limit")
        return int(value)

    def finite_float(value: str) -> float:
        number = float(value)
        if not math.isfinite(number):
            raise ProtocolError("json_number_limit")
        return number

    try:
        result = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                            parse_constant=bad_constant, parse_int=bounded_integer,
                            parse_float=finite_float)
    except ProtocolError:
        raise
    except (UnicodeError, ValueError, RecursionError):
        raise ProtocolError("invalid_json") from None
    pending = [(result, 0)]
    nodes = 0
    while pending:
        value, depth = pending.pop()
        nodes += 1
        if depth > 32 or nodes > 10000:
            raise ProtocolError("json_structure_limit")
        if isinstance(value, dict):
            pending.extend((child, depth + 1) for child in value.values())
        elif isinstance(value, list):
            pending.extend((child, depth + 1) for child in value)
    return result


def check_host(status: Any) -> None:
    if not isinstance(status, dict) or status.get("hostVersion") != HOST_VERSION:
        raise ProtocolError("unqualified_host_version")
    caps = status.get("capabilities")
    if (not isinstance(caps, list) or len(caps) > 64
            or any(not isinstance(v, str) or len(v) > 128 for v in caps)
            or not {"orderedReplicasV1", "sendAcceptanceV1"}.issubset(caps)
            or type(status.get("isBusy")) is not bool):
        raise ProtocolError("host_capability_mismatch")


def check_idle(health: Any, agent_id: str) -> None:
    if (not isinstance(health, dict) or health.get("ok") is not True
            or health.get("isBusy") is not False
            or health.get("activeAgentId") != agent_id
            or ("busyOnlyAwaitingApproval" in health
                and health["busyOnlyAwaitingApproval"] is not False)):
        raise ProtocolError("host_not_idle_for_target")


def prompt_digest(agent_id: str, nonce: str, prompt: str) -> str:
    _uuid(agent_id)
    _uuid(nonce)
    _text(prompt, MAX_PROMPT, "invalid_prompt")
    # Host canonicalSendInput/sendInputDigest: plain-text, no attachments,
    # richText, reply/fork or automation provenance. JS JSON.stringify UTF-8.
    raw = json.dumps([agent_id, prompt, None, None, False, None, [], []],
                     ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _entries(page: Any) -> list[dict[str, Any]]:
    if not isinstance(page, dict) or not isinstance(page.get("entries"), list):
        raise ProtocolError("invalid_transcript_page")
    rows = page["entries"]
    if len(rows) > MAX_ENTRIES or any(not isinstance(e, dict) for e in rows):
        raise ProtocolError("invalid_transcript_page")
    ids = [identifier(e.get("id")) for e in rows]
    if len(set(ids)) != len(ids):
        raise ProtocolError("duplicate_transcript_id")
    return rows


@dataclass(frozen=True)
class Baseline:
    last_id: str | None
    request_ids: tuple[str, ...]


def capture_baseline(page: Any) -> Baseline:
    rows = _entries(page)
    request_ids = tuple(sorted({identifier(e["requestId"]) for e in rows
                                if e.get("requestId") is not None}))
    return Baseline(rows[-1]["id"] if rows else None, request_ids)


@dataclass(frozen=True)
class AcceptedPrompt:
    agent_id: str
    nonce: str
    digest: str
    echo_id: str


def accepted_prompt(value: Any, agent_id: str, nonce: str, prompt: str) -> AcceptedPrompt:
    digest = prompt_digest(agent_id, nonce, prompt)
    if not isinstance(value, dict) or value.get("outcome") != "found":
        raise ProtocolError("acceptance_uncertain")
    record = value.get("record")
    if (not isinstance(record, dict) or record.get("accountSlot") != "host"
            or record.get("agentId") != agent_id or record.get("clientNonce") != nonce
            or record.get("inputDigest") != digest):
        raise ProtocolError("acceptance_binding_mismatch")
    if record.get("status") != "accepted":
        raise ProtocolError("acceptance_not_accepted")
    return AcceptedPrompt(agent_id, nonce, digest, identifier(record.get("echoEntryId")))


@dataclass(frozen=True)
class BoundReply:
    request_id: str
    entry_ids: tuple[str, ...]
    texts: tuple[str, ...]


def bound_reply(accepted: AcceptedPrompt, prompt: str, baseline: Baseline,
                page: Any, health: Any) -> BoundReply:
    """Validate a complete, bounded post-baseline range and an idle observation.

    A missing baseline/echo, foreign input, unsupported output or stale run is
    uncertain, not a reply. Never return the latest unbound assistant message.
    This does not certify external tool effects or prevent another app using
    the Bot; interference instead retires this operation.
    """
    check_idle(health, accepted.agent_id)
    if prompt_digest(accepted.agent_id, accepted.nonce, prompt) != accepted.digest:
        raise ProtocolError("prompt_changed")
    rows = _entries(page)
    if baseline.last_id is not None:
        found = [i for i, e in enumerate(rows) if e["id"] == baseline.last_id]
        if not found:
            raise ProtocolError("baseline_window_lost")
        rows = rows[found[0] + 1:]
    elif len(rows) >= MAX_ENTRIES or page.get("nextBeforeSeq") is not None:
        # With no pre-send anchor a full or paginated tail may have silently
        # dropped a concurrent input before our echo. Do not infer completeness.
        raise ProtocolError("unanchored_range_not_complete")
    echo = next((e for e in rows if e["id"] == accepted.echo_id), None)
    if (echo is None or echo.get("kind") != "message" or echo.get("role") != "user"
            or echo.get("clientNonce") != accepted.nonce or echo.get("content") != prompt
            or echo.get("isStreaming") is not False):
        raise ProtocolError("echo_binding_mismatch")
    request_id = identifier(echo.get("requestId"))
    if request_id in baseline.request_ids:
        raise ProtocolError("reused_request_id")
    if not rows or rows[0]["id"] != accepted.echo_id:
        raise ProtocolError("interleaved_input")
    texts: list[str] = []
    ids: list[str] = []
    for entry in rows[1:]:
        if entry.get("requestId") != request_id:
            raise ProtocolError("interleaved_run")
        # First slice accepts only visible text send-message records. Tool,
        # approval and unknown records need separately qualified handling.
        message = entry.get("message")
        if (entry.get("kind") != "send-message" or not isinstance(message, dict)
                or set(message) != {"type", "content"} or message.get("type") != "text"
                or entry.get("author") is not None
                or ("isStreaming" in entry and entry["isStreaming"] is not False)):
            raise ProtocolError("unsupported_reply_record")
        text = _text(message.get("content"), MAX_REPLY, "invalid_reply_text")
        texts.append(text)
        ids.append(entry["id"])
    if not texts or sum(len(t.encode("utf-8")) for t in texts) > MAX_REPLY:
        raise ProtocolError("reply_unavailable_or_oversize")
    return BoundReply(request_id, tuple(ids), tuple(texts))
