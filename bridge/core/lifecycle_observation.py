"""Provider-neutral lifecycle observation contract (#645).

Claude hook payloads and Codex app-server events have different shapes but carry
the same operational signal. This normalizes both into one versioned,
**body-free** ``LifecycleObservation`` — correlation ids are opaque hashes, tool
targets are reduced to a shape (``file``/``command``), and no prompt text, tool
argument, command, path, token, or message body is ever retained. A credential
in a prompt becomes a flag, never a stored value.

This module is a pure normalization + schema layer: no I/O, no live wiring. The
audit ledger sink (``lifecycle_audit``) persists these; feeding real hooks and
AgentEvents into the normalizers is separate, canary-gated wiring.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
import re
from typing import Any, Final, Iterable, Mapping

from telegram_bot.utils.redaction import contains_credential

SCHEMA_VERSION: Final = 1
_PROVIDERS: Final = ("claude", "codex")

# Tools whose completion is worth auditing are everything except clearly
# read-only tools — mirrors audit.sh's mutating filter while defaulting an
# unknown tool to auditable (conservative: never silently drop a mutation).
_READ_ONLY_TOKENS: Final = frozenset(
    {"read", "view", "cat", "head", "tail", "grep", "glob", "ls", "list",
     "search", "fetch", "show"}
)
_CAMEL_SPLIT_RE: Final = re.compile(r"([a-z0-9])([A-Z])")
# Codex app-server ``item`` types that are not tools.
_NON_TOOL_ITEM_TYPES: Final = frozenset(
    {"agentMessage", "userMessage", "reasoning", "plan",
     "enteredReviewMode", "exitedReviewMode"}
)
# Tools that change files (evidence-gate: a turn that changed files should show
# a verification signal). Snake/camel tokens.
_FILE_CHANGE_TOKENS: Final = frozenset(
    {"write", "edit", "multiedit", "notebookedit", "filechange", "patch",
     "applypatch", "change"}
)
# A verification action — mirrors claude/hooks/evidence-gate.sh's evidence regex.
# Runs against the command only to compute a boolean; the command is never stored.
_EVIDENCE_RE: Final = re.compile(
    r"\b(pytest|test|validate|verify|--dry-run|--check|shellcheck|bats|"
    r"gh pr checks|git diff|git status|lint|ruff|mypy|tsc|typecheck)\b",
    re.IGNORECASE,
)
_ISO_TS_RE: Final = re.compile(r"^\d{4}-\d{2}-\d{2}T[\d:.+Z-]{4,}$")


class LifecycleEventType(str, Enum):
    PROMPT_SUBMITTED = "prompt_submitted"
    TOOL_COMPLETED = "tool_completed"
    TURN_COMPLETED = "turn_completed"
    SESSION_CLOSED = "session_closed"
    PROVIDER_NOTIFICATION = "provider_notification"


def _tool_tokens(name: str) -> list[str]:
    spaced = _CAMEL_SPLIT_RE.sub(r"\1 \2", name).lower()
    return [tok for tok in re.split(r"[^a-z0-9]+", spaced) if tok]


def is_auditable_tool(name: str) -> bool:
    """A tool completion is auditable unless the tool is clearly read-only.

    Handles camelCase (``fileRead``) and snake_case (``file_read``) tool names.
    """
    if not name:
        return False
    return not any(tok in _READ_ONLY_TOKENS for tok in _tool_tokens(name))


def is_file_change_tool(name: str) -> bool:
    """True when the tool changes files (Write/Edit/…, fileChange, apply_patch)."""
    return any(tok in _FILE_CHANGE_TOKENS for tok in _tool_tokens(name))


def is_verification_command(command: object) -> bool:
    """True when a command text looks like a verification action. Body-free:
    only the boolean is used, the command is never stored."""
    return isinstance(command, str) and _EVIDENCE_RE.search(command) is not None


def _ref(value: object) -> str | None:
    """Opaque correlation ref: a short salted hash, never the raw id."""
    if value is None:
        return None
    text = str(value)
    if not text:
        return None
    return sha256(("ccc-lifecycle:" + text).encode("utf-8")).hexdigest()[:16]


def _clean_ts(value: object) -> str | None:
    return value if isinstance(value, str) and _ISO_TS_RE.match(value) else None


@dataclass(frozen=True, slots=True)
class LifecycleObservation:
    """One normalized, body-free lifecycle signal."""

    event: LifecycleEventType
    provider: str
    session_ref: str | None = None
    turn_ref: str | None = None
    tool: str | None = None
    tool_status: str | None = None       # "success" | "failure"
    target_shape: str | None = None      # "file" | "command" | None
    flag: str | None = None              # e.g. "possible-raw-credential"
    file_change: bool = False            # tool changed files (evidence-gate)
    verification: bool = False           # tool ran a verification action
    correlation: str | None = None       # opaque per-event id (dedup)
    observed_at: str | None = None
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.provider not in _PROVIDERS:
            raise ValueError(f"provider must be one of {_PROVIDERS}")

    def dedup_key(self) -> str:
        basis = "|".join(
            str(part) for part in (
                self.schema_version, self.provider, self.event.value,
                self.session_ref, self.turn_ref, self.tool, self.tool_status,
                self.correlation,
            )
        )
        return sha256(basis.encode("utf-8")).hexdigest()

    def to_record(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "schema_version": self.schema_version,
            "event": self.event.value,
            "provider": self.provider,
        }
        for key, value in (
            ("session_ref", self.session_ref), ("turn_ref", self.turn_ref),
            ("tool", self.tool), ("tool_status", self.tool_status),
            ("target_shape", self.target_shape), ("flag", self.flag),
            ("observed_at", self.observed_at),
        ):
            if value is not None:
                record[key] = value
        for key, flag in (("file_change", self.file_change), ("verification", self.verification)):
            if flag:
                record[key] = True
        return record


# --------------------------------------------------------------------------- #
# Claude hook normalization
# --------------------------------------------------------------------------- #

def normalize_claude_hook(
    event_name: str, payload: Mapping[str, Any]
) -> LifecycleObservation | None:
    """Normalize a Claude Code hook stdin payload. Returns None when the event
    carries no auditable signal (e.g. a read-only tool)."""

    if not isinstance(payload, Mapping):
        return None
    session_ref = _ref(payload.get("session_id"))
    ts = _clean_ts(payload.get("timestamp"))

    if event_name == "PostToolUse":
        tool = str(payload.get("tool_name") or "")
        if not is_auditable_tool(tool):
            return None
        tool_input = payload.get("tool_input")
        target_shape = None
        command = ""
        if isinstance(tool_input, Mapping):
            command = str(tool_input.get("command") or "")
            if command:
                target_shape = "command"
            elif tool_input.get("file_path"):
                target_shape = "file"
        response = payload.get("tool_response")
        success = True
        if isinstance(response, Mapping) and response.get("success") is False:
            success = False
        return LifecycleObservation(
            event=LifecycleEventType.TOOL_COMPLETED, provider="claude",
            session_ref=session_ref, tool=tool,
            tool_status="success" if success else "failure",
            target_shape=target_shape,
            file_change=is_file_change_tool(tool) or target_shape == "file",
            verification=is_verification_command(command),
            correlation=_ref(f"{payload.get('session_id')}:{tool}:{target_shape}:{ts}"),
            observed_at=ts,
        )
    if event_name == "UserPromptSubmit":
        prompt = payload.get("prompt") or payload.get("user_prompt") or ""
        flag = "possible-raw-credential" if contains_credential(str(prompt)) else None
        return LifecycleObservation(
            event=LifecycleEventType.PROMPT_SUBMITTED, provider="claude",
            session_ref=session_ref, flag=flag,
            correlation=_ref(f"{payload.get('session_id')}:prompt:{ts}"), observed_at=ts,
        )
    if event_name == "Stop":
        return LifecycleObservation(
            event=LifecycleEventType.TURN_COMPLETED, provider="claude",
            session_ref=session_ref,
            correlation=_ref(f"{payload.get('session_id')}:stop:{ts}"), observed_at=ts,
        )
    if event_name == "SessionEnd":
        return LifecycleObservation(
            event=LifecycleEventType.SESSION_CLOSED, provider="claude",
            session_ref=session_ref,
            correlation=_ref(f"{payload.get('session_id')}:end"), observed_at=ts,
        )
    if event_name == "Notification":
        return LifecycleObservation(
            event=LifecycleEventType.PROVIDER_NOTIFICATION, provider="claude",
            session_ref=session_ref, flag="notification",
            correlation=_ref(f"{payload.get('session_id')}:notify:{ts}"), observed_at=ts,
        )
    return None


# --------------------------------------------------------------------------- #
# Codex app-server event normalization
# --------------------------------------------------------------------------- #

def normalize_codex_app_server(
    notification: Mapping[str, Any]
) -> LifecycleObservation | None:
    """Normalize a raw Codex app-server notification (``{method, params}``).
    Returns None for events with no auditable lifecycle signal or that the
    provider does not offer (never guessed)."""

    if not isinstance(notification, Mapping):
        return None
    method = str(notification.get("method") or "")
    params = notification.get("params")
    if not isinstance(params, Mapping):
        params = {}
    session_ref = _ref(params.get("threadId"))
    turn_ref = _ref(params.get("turnId"))
    ts = _clean_ts(params.get("timestamp"))

    if method == "item/completed":
        item = params.get("item")
        if not isinstance(item, Mapping):
            return None
        item_type = str(item.get("type") or "")
        if not item_type or item_type in _NON_TOOL_ITEM_TYPES:
            return None
        if not is_auditable_tool(item_type):
            return None
        status = str(item.get("status") or "")
        exit_code = item.get("exitCode")
        success = status in ("completed", "success") and exit_code in (None, 0)
        command = str(item.get("command") or "")
        return LifecycleObservation(
            event=LifecycleEventType.TOOL_COMPLETED, provider="codex",
            session_ref=session_ref, turn_ref=turn_ref, tool=item_type,
            tool_status="success" if success else "failure",
            file_change=is_file_change_tool(item_type),
            verification=is_verification_command(command),
            correlation=_ref(item.get("id")) or _ref(f"{params.get('turnId')}:{item_type}"),
            observed_at=ts,
        )
    if method == "turn/started":
        return LifecycleObservation(
            event=LifecycleEventType.PROMPT_SUBMITTED, provider="codex",
            session_ref=session_ref, turn_ref=turn_ref,
            correlation=_ref(f"{params.get('turnId')}:start"), observed_at=ts,
        )
    if method == "turn/completed":
        turn = params.get("turn")
        status = str(turn.get("status")) if isinstance(turn, Mapping) else ""
        return LifecycleObservation(
            event=LifecycleEventType.TURN_COMPLETED, provider="codex",
            session_ref=session_ref, turn_ref=turn_ref,
            tool_status="success" if status == "completed" else "failure",
            correlation=_ref(f"{params.get('turnId')}:done"), observed_at=ts,
        )
    if method.endswith("requestApproval"):
        return LifecycleObservation(
            event=LifecycleEventType.PROVIDER_NOTIFICATION, provider="codex",
            session_ref=session_ref, turn_ref=turn_ref, flag="approval",
            correlation=_ref(f"{params.get('turnId')}:{method}"), observed_at=ts,
        )
    return None


# --------------------------------------------------------------------------- #
# Evidence gate (body-free, provider-neutral)
# --------------------------------------------------------------------------- #

@dataclass(frozen=True, slots=True)
class EvidenceVerdict:
    needs_evidence: bool
    file_changes: int
    verifications: int


def evidence_gate(
    observations: "Iterable[LifecycleObservation]",
) -> EvidenceVerdict:
    """Provider-neutral evidence gate over one session's observations.

    A turn that changed files but ran no verification action
    (test/validate/verify/diff/lint/…) needs evidence. Pure query over the
    body-free ``file_change``/``verification`` flags — no command text, no
    delivery, no provider recursion; the caller decides how to surface it.
    """

    file_changes = 0
    verifications = 0
    for obs in observations:
        if getattr(obs, "event", None) is not LifecycleEventType.TOOL_COMPLETED:
            continue
        if getattr(obs, "file_change", False):
            file_changes += 1
        if getattr(obs, "verification", False):
            verifications += 1
    return EvidenceVerdict(
        needs_evidence=file_changes > 0 and verifications == 0,
        file_changes=file_changes,
        verifications=verifications,
    )


# --------------------------------------------------------------------------- #
# Live AgentEvent normalization (both providers converge on AgentRuntime events)
# --------------------------------------------------------------------------- #

def normalize_agent_event(
    *, provider: str, session_id: object, event: object
) -> LifecycleObservation | None:
    """Normalize a provider-neutral ``AgentEvent`` (the live bridge seam).

    Duck-typed by class name to avoid importing the runtime event module into
    this low-level layer. Only tool/turn/approval events carry lifecycle signal;
    everything else returns None.
    """

    kind = type(event).__name__
    session_ref = _ref(session_id)

    if kind == "ToolCompletedEvent":
        tool = str(getattr(event, "tool_name", "") or "")
        if not is_auditable_tool(tool):
            return None
        success = bool(getattr(event, "success", False))
        # Best-effort verification signal from the tool arguments when the event
        # carries them (ToolCompletedEvent may not); body-free boolean only.
        args = getattr(event, "arguments", None) or getattr(event, "result", None)
        command = args.get("command") if isinstance(args, Mapping) else ""
        return LifecycleObservation(
            event=LifecycleEventType.TOOL_COMPLETED, provider=provider,
            session_ref=session_ref, tool=tool,
            tool_status="success" if success else "failure",
            file_change=is_file_change_tool(tool),
            verification=is_verification_command(command),
            correlation=_ref(getattr(event, "tool_call_id", None)) or _ref(f"{session_id}:{tool}"),
        )
    if kind == "CompletionEvent":
        stop = str(getattr(event, "stop_reason", "") or "")
        return LifecycleObservation(
            event=LifecycleEventType.TURN_COMPLETED, provider=provider,
            session_ref=session_ref,
            tool_status="success" if stop in ("end_turn", "completed") else "failure",
            correlation=_ref(f"{session_id}:turn:{stop}"),
        )
    if kind == "ApprovalRequestEvent":
        return LifecycleObservation(
            event=LifecycleEventType.PROVIDER_NOTIFICATION, provider=provider,
            session_ref=session_ref, flag="approval",
            correlation=_ref(getattr(event, "request_id", None)),
        )
    return None


__all__ = [
    "SCHEMA_VERSION",
    "LifecycleEventType",
    "LifecycleObservation",
    "EvidenceVerdict",
    "is_auditable_tool",
    "is_file_change_tool",
    "is_verification_command",
    "evidence_gate",
    "normalize_claude_hook",
    "normalize_codex_app_server",
    "normalize_agent_event",
]
