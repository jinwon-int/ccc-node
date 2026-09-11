"""Aggregated, read-only node status behind the ``node_status`` CLI/MCP tool.

One call collects what used to take several shell probes (#1694): serving
checkout state and bridge process (``ccc-bridge-locate.sh --json``), service/
transport/provider health (``bridge/start.sh --status`` text, parsed), and
scheduler/task occupancy (``agent-cron.sh status --json``).  The module is
stdlib-only so it also runs remotely over ssh with any python3.

Principles from issue #1694: every section carries ``status: ok|unknown``
plus its observation latency; a failed or unparseable collector becomes
``unknown`` — never a fabricated value; nothing here mutates state, and the
result never substitutes for approvals or live re-verification at execution
time.  Model identity has no structured source yet and is reported as
``unknown`` rather than guessed.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import socket
import subprocess
from pathlib import Path
from typing import Any, Callable, Mapping

RUNNER = Callable[[list[str], float], "subprocess.CompletedProcess[bytes]"]

COLLECT_TIMEOUTS = {"locate": 10.0, "service": 25.0, "scheduler": 25.0}
DEFAULT_REMOTE_PATH = "/opt/ccc-node/scripts/ccc-node-status.py"
SSH_TIMEOUT = 45.0
_MAX_OUTPUT_BYTES = 1_000_000


class NodeStatusError(ValueError):
    """A structured node-status failure (CLI/ToolError boundary)."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details

    def payload(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": str(self), **self.details}}


def _repo_root(env: Mapping[str, str] | None = None) -> Path:
    environment = os.environ if env is None else env
    configured = str(environment.get("CCC_SKILL_LOOKUP_REPO_ROOT", "")).strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


def _collector_cmd(name: str, default: Path, env: Mapping[str, str] | None) -> list[str]:
    """Collector command, overridable for tests via CCC_NODE_STATUS_* env."""

    environment = os.environ if env is None else env
    override = str(environment.get(f"CCC_NODE_STATUS_{name.upper()}", "")).strip()
    if override:
        return [part for part in override.split(" ") if part]
    return ["bash", str(default)]


def _default_runner(cmd: list[str], timeout: float) -> "subprocess.CompletedProcess[bytes]":
    return subprocess.run(cmd, capture_output=True, timeout=timeout)


def _run_section(
    name: str,
    cmd: list[str],
    runner: RUNNER | None,
) -> tuple[dict[str, Any], str]:
    """Run one collector; returns (section_status, decoded stdout or b'')."""

    started = _dt.datetime.now(_dt.timezone.utc)
    try:
        completed = (runner or _default_runner)(cmd, COLLECT_TIMEOUTS[name])
    except subprocess.TimeoutExpired:
        return {"status": "unknown", "error": "timeout"}, ""
    except OSError as error:
        return {"status": "unknown", "error": f"unavailable: {error}"}, ""
    latency_ms = int((_dt.datetime.now(_dt.timezone.utc) - started).total_seconds() * 1000)
    stderr = completed.stderr.decode("utf-8", "replace")[-500:]
    if completed.returncode != 0:
        return {"status": "unknown", "error": f"exit {completed.returncode}", "stderr_tail": stderr}, ""
    stdout = completed.stdout.decode("utf-8", "replace")
    if len(stdout) > _MAX_OUTPUT_BYTES:
        return {"status": "unknown", "error": "output exceeds the size bound"}, ""
    return {"status": "ok", "latency_ms": latency_ms}, stdout


def _parse_bridge_status(text: str) -> dict[str, Any]:
    """Parse the ``start.sh --status`` text into body-free health fields."""

    fields: dict[str, str] = {}
    for line in text.splitlines():
        cleaned = line.replace("🟢", " ").replace("🔴", " ").replace("🟡", " ").strip()
        match = re.match(r"^(Bot status|Process|Service|Turn occupancy|Telegram|Piri)\s*:\s*(.+)$", cleaned)
        if match:
            fields[match.group(1)] = match.group(2).strip()
    healthy = {"available", "healthy"}
    bot_status = fields.get("Bot status", "").lower()
    return {
        "bot_status": fields.get("Bot status", "unknown"),
        "service": fields.get("Service", "unknown"),
        "process": fields.get("Process", "unknown"),
        "turn_occupancy": fields.get("Turn occupancy", "unknown"),
        "telegram": fields.get("Telegram", "unknown"),
        "piri": fields.get("Piri", "unknown"),
        "healthy": bot_status in healthy and fields.get("Service", "").lower() in healthy,
    }


def collect_local(
    *,
    env: Mapping[str, str] | None = None,
    runner: RUNNER | None = None,
    project_path: str | None = None,
) -> dict[str, Any]:
    """Collect this node's status; never raises for collector failures."""

    environment = os.environ if env is None else env
    repo = _repo_root(environment)
    sections: dict[str, Any] = {}
    partial = False

    section, stdout = _run_section(
        "locate",
        _collector_cmd("locate", repo / "scripts" / "ccc-bridge-locate.sh", environment) + ["--json"],
        runner,
    )
    located: dict[str, Any] = {}
    if section["status"] == "ok":
        try:
            document = json.loads(stdout)
            bridges = document.get("bridges") or []
            located = bridges[0] if bridges else {}
            section["running"] = bool(document.get("running"))
        except json.JSONDecodeError:
            section = {"status": "unknown", "error": "unparseable locate output"}
    if section["status"] != "ok":
        partial = True
    source = {
        "head": located.get("head", "unknown"),
        "branch": located.get("branch", "unknown"),
        "dirty": located.get("dirty", "unknown"),
        "checkout": located.get("checkout", "unknown"),
        **section,
    }
    sections["source"] = source
    bridge = {
        "running": located.get("running", section.get("running", "unknown")),
        "pid": located.get("pid", "unknown"),
        **{key: value for key, value in section.items()},
    }
    sections["bridge_process"] = bridge

    default_project = str(environment.get("CCC_BRIDGE_DEFAULT_PATH", "")).strip() or str(Path.home())
    section, stdout = _run_section(
        "service",
        _collector_cmd("bridge_status", repo / "bridge" / "start.sh", environment)
        + ["--path", project_path or default_project, "--status"],
        runner,
    )
    service: dict[str, Any] = {**section}
    if section["status"] == "ok":
        service.update(_parse_bridge_status(stdout))
    else:
        partial = True
    sections["service"] = service

    section, stdout = _run_section(
        "scheduler",
        _collector_cmd("agent_cron", repo / "scripts" / "agent-cron.sh", environment) + ["status", "--json"],
        runner,
    )
    scheduler: dict[str, Any] = {**section}
    if section["status"] == "ok":
        try:
            document = json.loads(stdout)
            scheduler["summary"] = document.get("summary", {})
            scheduler["at"] = document.get("at", "unknown")
            scheduler["errors"] = document.get("errors", [])
        except json.JSONDecodeError:
            scheduler.update({"status": "unknown", "error": "unparseable scheduler output"})
            partial = True
    else:
        partial = True
    sections["scheduler"] = scheduler

    # No structured model-identity source exists yet (#1694): report unknown.
    sections["model"] = {"status": "unknown", "reason": "no structured source"}

    return {
        "observed_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "node": socket.gethostname(),
        "repo_root": str(repo),
        "status": "unknown" if partial else "ok",
        "sections": sections,
    }


def collect_remote(
    node: str,
    *,
    env: Mapping[str, str] | None = None,
    runner: RUNNER | None = None,
) -> dict[str, Any]:
    """Collect one peer node's status over ssh; failures stay node-scoped."""

    environment = os.environ if env is None else env
    if not node or any(ch in node for ch in " \t;\n&|`$"):
        raise NodeStatusError("invalid_node", "node must be an ssh host alias", node=node)
    remote_path = str(environment.get("CCC_NODE_STATUS_REMOTE_PATH", "")).strip() or DEFAULT_REMOTE_PATH
    ssh_cmd = str(environment.get("CCC_NODE_STATUS_SSH", "")).strip()
    cmd = [
        *(ssh_cmd.split() or ["ssh"]),
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "--",
        node,
        f"python3 {remote_path} --local",
    ]
    observed_at = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        completed = (runner or _default_runner)(cmd, SSH_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"node": node, "observed_at": observed_at, "status": "unknown", "error": "timeout"}
    except OSError as error:
        return {"node": node, "observed_at": observed_at, "status": "unknown", "error": f"ssh unavailable: {error}"}
    stdout = completed.stdout.decode("utf-8", "replace")
    if completed.returncode != 0:
        return {
            "node": node,
            "observed_at": observed_at,
            "status": "unknown",
            "error": f"exit {completed.returncode}",
            "stderr_tail": completed.stderr.decode("utf-8", "replace")[-500:],
        }
    try:
        result = json.loads(stdout)
    except json.JSONDecodeError:
        return {"node": node, "observed_at": observed_at, "status": "unknown", "error": "unparseable remote output"}
    return {"node": node, "observed_at": observed_at, "status": result.get("status", "unknown"), "result": result}


def node_status(
    nodes: list[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    runner: RUNNER | None = None,
    project_path: str | None = None,
) -> dict[str, Any]:
    """Local status, or an aggregation over the given ssh node aliases."""

    try:
        from telegram_bot.core.skill_lookup import policy_denial
    except ImportError:  # pragma: no cover - worktree aliasing only
        from skill_lookup import policy_denial

    denial = policy_denial(env)
    if denial is not None:
        raise NodeStatusError("policy_denied", "node status denied by node policy", reason=denial)
    if not nodes:
        return collect_local(env=env, runner=runner, project_path=project_path)
    results = [collect_remote(node, env=env, runner=runner) for node in nodes]
    aggregated_status = "ok" if all(item["status"] == "ok" for item in results) else "unknown"
    return {
        "observed_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "nodes": results,
        "status": aggregated_status,
    }
