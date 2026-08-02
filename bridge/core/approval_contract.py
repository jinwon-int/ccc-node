"""Provider-neutral, body-safe approval snapshots (#870).

The keyed request fingerprint binds the complete provider payload in memory so
persisted audit hashes do not become a dictionary oracle for low-entropy input.
The ordinary SHA-256 display fingerprint binds the already-redacted, bounded
text shown to the owner and remains independently verifiable after restart.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import hmac
import json
import math
import re
import secrets
import unicodedata
from typing import Any

from telegram_bot.core.agent_runtime import ApprovalRequestEvent
from telegram_bot.utils.redaction import REDACTION_MARKER, redact_credentials

_FINGERPRINT_KEY = secrets.token_bytes(32)
_ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_QUOTED_AUTHORIZATION = re.compile(
    r'''(?P<quote>["'])\s*authorization\s*[:=][^\r\n]*?(?P=quote)''',
    re.IGNORECASE,
)
_AUTHORIZATION = re.compile(r"\bauthorization\s*[:=][^\r\n;]*", re.IGNORECASE)
_ENV_ASSIGNMENT = re.compile(
    r'''(?<![\w])([A-Za-z_][A-Za-z0-9_]{0,63})=("[^"\r\n]*"|'[^'\r\n]*'|[^\s]+)'''
)
_SENSITIVE_LONG_OPTION = re.compile(
    r'''(?<![\w])(--(?:[a-z0-9]+[-_])*(?:api[-_]?key|token|password|'''
    r'''passphrase|secret|credential|authorization|private[-_]?key|cookie)'''
    r'''(?:[-_][a-z0-9]+)*)(?:\s+|=)("[^"\r\n]*"|'[^'\r\n]*'|[^\s]+)''',
    re.IGNORECASE,
)
_SHORT_PASSWORD_OPTION = re.compile(
    r'''(?<![\w])(-p)(?:\s+|=)?("[^"\r\n]*"|'[^'\r\n]*'|[^\s]+)''',
    re.IGNORECASE,
)
_SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "content",
        "contents",
        "data",
        "diff",
        "env",
        "environment",
        "file_content",
        "headers",
        "new_string",
        "old_string",
        "patch",
        "password",
        "secret",
        "token",
    }
)
_COMMAND_ACTIONS = frozenset(
    {"item/commandexecution/requestapproval", "bash", "shell", "command"}
)
_FILE_ACTIONS = frozenset(
    {
        "item/filechange/requestapproval",
        "edit",
        "multiedit",
        "notebookedit",
        "write",
    }
)
_PERMISSION_ACTIONS = frozenset({"item/permissions/requestapproval"})
_PATH_KEYS = ("path", "file_path", "filePath", "paths", "target", "targets")
_CWD_KEYS = ("cwd", "working_directory", "workingDirectory")
_COMMAND_KEYS = ("command", "cmd", "script")


def _plain_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in sorted(value.items())}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_plain_json(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError("approval request arguments must be JSON values")


def _fingerprint(value: object) -> str:
    payload = json.dumps(
        _plain_json(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hmac.new(_FINGERPRINT_KEY, payload, hashlib.sha256).hexdigest()
    return f"hmac-sha256:{digest}"


def _display_fingerprint(value: str) -> str:
    """Hash already-redacted owner text so it can be verified after restart."""
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def opaque_ref(*parts: object) -> str:
    """Create a process-scoped opaque reference without storing raw identifiers."""
    return _fingerprint([_plain_json(part) for part in parts])


def _strip_unsafe_controls(value: str) -> tuple[str, bool]:
    stripped = _ANSI_ESCAPE.sub("", value).replace("\u2028", "\n").replace("\u2029", "\n")
    cleaned = "".join(
        character
        for character in stripped
        if character in "\n\t" or not unicodedata.category(character).startswith("C")
    )
    return cleaned, cleaned != value


def _truncate_utf8(value: str, limit: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value, False
    clipped = encoded[: max(0, limit - len("…".encode("utf-8")))]
    return clipped.decode("utf-8", errors="ignore").rstrip() + "…", True


def _safe_text(
    value: object,
    *,
    limit: int,
    first_line_only: bool = False,
) -> tuple[str, set[str]]:
    flags: set[str] = set()
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        text = " ".join(str(item) for item in value[:8])
    elif isinstance(value, str):
        text = value
    else:
        text = str(value)
    text, controls_removed = _strip_unsafe_controls(text)
    if controls_removed:
        flags.add("controls_removed")
    if first_line_only and ("\n" in text or "\r" in text):
        text = text.splitlines()[0] + " …"
        flags.add("body_omitted")
    authorization_present = bool(
        _QUOTED_AUTHORIZATION.search(text) or _AUTHORIZATION.search(text)
    )
    redacted = redact_credentials(text)
    if redacted != text:
        flags.add("credential_redacted")
    text = _QUOTED_AUTHORIZATION.sub(
        lambda match: f"{match.group('quote')}{REDACTION_MARKER}{match.group('quote')}",
        redacted,
    )
    text = _AUTHORIZATION.sub(f"Authorization: {REDACTION_MARKER}", text)
    if authorization_present:
        flags.add("authorization_redacted")

    def redact_long_option(match: re.Match[str]) -> str:
        flags.add("credential_redacted")
        return f"{match.group(1)} {REDACTION_MARKER}"

    text = _SENSITIVE_LONG_OPTION.sub(redact_long_option, text)
    text = _SHORT_PASSWORD_OPTION.sub(redact_long_option, text)

    def redact_assignment(match: re.Match[str]) -> str:
        flags.add("environment_redacted")
        return f"{match.group(1)}=[REDACTED_ENV]"

    text = _ENV_ASSIGNMENT.sub(redact_assignment, text)
    text = " ".join(text.split())
    text, truncated = _truncate_utf8(text, limit)
    if truncated:
        flags.add("truncated")
    return text or "(not supplied)", flags


def _argument(arguments: Mapping[str, object], keys: Sequence[str]) -> object | None:
    for key in keys:
        if key in arguments:
            return arguments[key]
    return None


def _provider_and_shape(action: str) -> tuple[str, str, str]:
    normalized = action.casefold()
    provider = "codex" if normalized.startswith("item/") else "claude"
    if normalized in _COMMAND_ACTIONS:
        return provider, "command_execution", "command"
    if normalized in _FILE_ACTIONS:
        return provider, "file_change", "file"
    if normalized in _PERMISSION_ACTIONS:
        return provider, "permissions", "permission-set"
    return provider, "tool_use", "tool"


def _summary(
    action: str,
    target_shape: str,
    arguments: Mapping[str, object],
) -> tuple[str, set[str]]:
    if target_shape == "command":
        command = _argument(arguments, _COMMAND_KEYS)
        if command is None:
            return "command details unavailable", set()
        return _safe_text(command, limit=240, first_line_only=True)
    if target_shape == "file":
        path = _argument(arguments, _PATH_KEYS)
        flags = {"body_omitted"} if any(
            str(key).casefold() in _SENSITIVE_KEYS for key in arguments
        ) else set()
        if path is None:
            return "file change target unavailable", flags
        summary, path_flags = _safe_text(path, limit=200, first_line_only=True)
        return summary, flags | path_flags
    if target_shape == "permission-set":
        permissions = arguments.get("permissions")
        if isinstance(permissions, Mapping):
            names = ", ".join(sorted(str(key) for key in permissions)[:12])
        elif isinstance(permissions, Sequence) and not isinstance(permissions, str):
            names = ", ".join(str(item) for item in permissions[:12])
        else:
            names = "permission details unavailable"
        return _safe_text(names, limit=200, first_line_only=True)
    return _safe_text(action, limit=120, first_line_only=True)


def _risk_hints(action: str, summary: str, cwd_hint: str | None) -> tuple[str, ...]:
    haystack = f"{action} {summary} {cwd_hint or ''}".casefold()
    hints: list[str] = []
    if any(marker in haystack for marker in ("http://", "https://", "curl ", "wget ", "network", "webfetch")):
        hints.append("network")
    if re.search(r"(?:^|\s)(?:sudo|su)(?:\s|$)", haystack):
        hints.append("privileged")
    if re.search(r"(?:^|\s)(?:rm\s+-[^\s]*r|shred|mkfs|git\s+reset\s+--hard)", haystack):
        hints.append("destructive")
    if summary.startswith("/") or (cwd_hint or "").startswith("/"):
        hints.append("absolute-path")
    return tuple(hints)


@dataclass(frozen=True, slots=True)
class ApprovalDisplaySnapshot:
    schema_version: int
    provider: str
    action: str
    target_shape: str
    summary: str
    cwd_hint: str | None
    risk_hints: tuple[str, ...]
    request_fingerprint: str
    display_fingerprint: str
    redaction_flags: tuple[str, ...]
    displayed_fields: tuple[str, ...]
    prompt_text: str


def build_approval_snapshot(event: ApprovalRequestEvent) -> ApprovalDisplaySnapshot:
    """Return a deterministic request binding and exact safe owner-facing text."""
    arguments = {str(key): value for key, value in event.arguments.items()}
    provider, action, target_shape = _provider_and_shape(event.action)
    summary, flags = _summary(action, target_shape, arguments)
    cwd_value = _argument(arguments, _CWD_KEYS)
    cwd_hint: str | None = None
    if cwd_value is not None:
        cwd_hint, cwd_flags = _safe_text(cwd_value, limit=120, first_line_only=True)
        flags.update(cwd_flags)
    if any(str(key).casefold() in _SENSITIVE_KEYS for key in arguments):
        flags.add("sensitive_fields_omitted")
    risks = _risk_hints(event.action, summary, cwd_hint)
    request_fingerprint = _fingerprint(
        {"provider": provider, "action": event.action, "arguments": arguments}
    )
    provider_label = "Codex" if provider == "codex" else "Claude"
    lines = [
        f"{provider_label} approval request",
        f"Action: {action.replace('_', ' ')}",
        f"Target: {target_shape}",
        f"Summary: {summary}",
    ]
    displayed_fields = ["provider", "action", "target_shape", "summary"]
    if cwd_hint:
        lines.append(f"Working directory: {cwd_hint}")
        displayed_fields.append("cwd_hint")
    if risks:
        lines.append(f"Risk hints: {', '.join(risks)}")
        displayed_fields.append("risk_hints")
    lines.append("Reply with 승인 or 거절, or use the buttons.")
    prompt_text = "\n".join(lines)
    display_fingerprint = _display_fingerprint(prompt_text)
    return ApprovalDisplaySnapshot(
        schema_version=1,
        provider=provider,
        action=action,
        target_shape=target_shape,
        summary=summary,
        cwd_hint=cwd_hint,
        risk_hints=risks,
        request_fingerprint=request_fingerprint,
        display_fingerprint=display_fingerprint,
        redaction_flags=tuple(sorted(flags)),
        displayed_fields=tuple(displayed_fields),
        prompt_text=prompt_text,
    )


__all__ = [
    "ApprovalDisplaySnapshot",
    "build_approval_snapshot",
    "opaque_ref",
]
