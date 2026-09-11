"""Task recovery status behind the ``task_status`` tool/CLI (#1694 item 2).

One read-only call aggregates what an operator or a fresh session needs to
resume work at low cost:

- ``working_state`` — the agent-written checkpoint (objective / progress /
  next step) from ``$CCC_STATE_DIR/working-state.md`` or
  ``~/.claude/state/working-state.md``,
- ``resume`` — the resume note (``CCC_RESUME_FILE`` / ``resume.md``),
- ``waits`` — external wait promises: active (pending) and dropped
  (terminal but never resumed), via the external_wait CLI.

stdlib-only; runs remotely like ``node_status``.  Section bodies are bounded
(truncated content is flagged, never presented as complete), every section
carries ``status: ok|unknown`` and observation latency, and the result never
substitutes for approvals or live re-verification.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Mapping

MAX_CONTENT_BYTES = 8 * 1024
WAITS_TIMEOUT = 30.0
_TERMINAL_STATES = {"success", "failure", "superseded"}
_SUMMARY_TAIL_CHARS = 160

RUNNER = Callable[[list[str], float], "subprocess.CompletedProcess[bytes]"]


class TaskStatusError(ValueError):
    """A structured task-status failure (CLI/ToolError boundary)."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details

    def payload(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": str(self), **self.details}}


def _repo_root(env: Mapping[str, str] | None) -> Path:
    environment = os.environ if env is None else env
    configured = str(environment.get("CCC_SKILL_LOOKUP_REPO_ROOT", "")).strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


def _state_dir(env: Mapping[str, str] | None) -> Path:
    environment = os.environ if env is None else env
    state_dir = str(environment.get("CCC_STATE_DIR", "")).strip()
    if state_dir:
        return Path(state_dir).expanduser()
    home = str(environment.get("CCC_SKILL_LOOKUP_HOME", "")).strip()
    return (Path(home).expanduser() if home else Path.home()) / ".claude" / "state"


def _read_bounded(path: Path) -> dict[str, Any]:
    """Read one note file with a hard bound; missing is ok, broken is unknown."""

    try:
        stat_result = path.stat()
    except FileNotFoundError:
        return {"status": "ok", "present": False}
    except OSError as error:
        return {"status": "unknown", "present": False, "error": f"unreadable: {error}"}
    section: dict[str, Any] = {
        "present": True,
        "path": str(path),
        "size_bytes": stat_result.st_size,
        "modified_at": _dt.datetime.fromtimestamp(
            stat_result.st_mtime, tz=_dt.timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "age_hours": round(
            max(0.0, (_dt.datetime.now(_dt.timezone.utc).timestamp() - stat_result.st_mtime) / 3600),
            1,
        ),
    }
    try:
        payload = path.read_bytes()
    except OSError as error:
        return {**section, "status": "unknown", "error": f"unreadable: {error}"}
    if b"\x00" in payload:
        return {**section, "status": "unknown", "error": "binary content"}
    text = payload.decode("utf-8", "replace")
    section["truncated"] = len(payload) > MAX_CONTENT_BYTES
    section["content_bytes"] = min(len(payload), MAX_CONTENT_BYTES)
    section["content"] = text[:MAX_CONTENT_BYTES]
    section["status"] = "ok"
    return section


def _default_runner(cmd: list[str], timeout: float, cwd: Path) -> "subprocess.CompletedProcess[bytes]":
    return subprocess.run(cmd, capture_output=True, timeout=timeout, cwd=cwd)


def _waits_section(env: Mapping[str, str] | None, runner: RUNNER | None) -> dict[str, Any]:
    environment = os.environ if env is None else env
    override = str(environment.get("CCC_TASK_STATUS_WAITS_CMD", "")).strip()
    if override:
        cmd = override.split()
    else:
        cmd = [sys.executable, "-m", "telegram_bot.core.external_wait_cli", "list"]
    started = _dt.datetime.now(_dt.timezone.utc)
    try:
        if runner is not None:
            completed = runner(cmd, WAITS_TIMEOUT)
        else:
            completed = _default_runner(cmd, WAITS_TIMEOUT, _repo_root(environment))
    except subprocess.TimeoutExpired:
        return {"status": "unknown", "error": "timeout"}
    except OSError as error:
        return {"status": "unknown", "error": f"unavailable: {error}"}
    latency_ms = int((_dt.datetime.now(_dt.timezone.utc) - started).total_seconds() * 1000)
    if completed.returncode != 0:
        return {
            "status": "unknown",
            "error": f"exit {completed.returncode}",
            "stderr_tail": completed.stderr.decode("utf-8", "replace")[-300:],
        }
    try:
        document = json.loads(completed.stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return {"status": "unknown", "error": "unparseable waits output"}
    if not isinstance(document, dict) or not isinstance(document.get("waits"), list):
        return {"status": "unknown", "error": "waits output shape invalid"}

    active: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for record in document["waits"]:
        if not isinstance(record, dict):
            continue
        entry = {
            "wait_id": record.get("wait_id", "unknown"),
            "repo": record.get("repo", "unknown"),
            "pr": record.get("pr"),
            "head_sha": record.get("head_sha", "unknown"),
            "state": record.get("state", "unknown"),
            "summary_tail": str(record.get("summary", ""))[-_SUMMARY_TAIL_CHARS:],
        }
        if str(record.get("state", "")) not in _TERMINAL_STATES:
            active.append(entry)
        elif record.get("resumed") is False:
            entry["skip_reason"] = record.get("skip_reason")
            dropped.append(entry)
    return {
        "status": "ok",
        "latency_ms": latency_ms,
        "total": len(document["waits"]),
        "active": active,
        "dropped": dropped,
    }


def collect(
    *,
    env: Mapping[str, str] | None = None,
    runner: RUNNER | None = None,
) -> dict[str, Any]:
    """Collect task-recovery status; collector failures become unknown."""

    try:
        from telegram_bot.core.skill_lookup import policy_denial
    except ImportError:  # pragma: no cover - worktree aliasing only
        from skill_lookup import policy_denial

    denial = policy_denial(env)
    if denial is not None:
        raise TaskStatusError("policy_denied", "task status denied by node policy", reason=denial)

    state_dir = _state_dir(env)
    sections: dict[str, Any] = {
        "working_state": _read_bounded(state_dir / "working-state.md"),
        "resume": _read_bounded(state_dir / "resume.md"),
        "waits": _waits_section(env, runner),
    }
    partial = any(section["status"] != "ok" for section in sections.values())
    return {
        "observed_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "node": socket.gethostname(),
        "status": "unknown" if partial else "ok",
        "sections": sections,
    }
