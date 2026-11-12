"""Aggregated, read-only node status behind the ``node_status`` CLI/MCP tool.

One call collects what used to take several shell probes (#1694): serving
checkout state and bridge process (``ccc-bridge-locate.sh --json``), service/
transport/provider health (``bridge/start.sh --status`` text, parsed), and
scheduler/task occupancy (``agent-cron.sh status --json``).  On a node whose
frontend list (#2342) retires Telegram, service health comes from the declared
frontends' own ``health.json`` instead (#2771).  The module is
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
import shlex
import socket
import subprocess
from pathlib import Path
from typing import Any, Callable, Mapping

RUNNER = Callable[[list[str], float], "subprocess.CompletedProcess[bytes]"]

COLLECT_TIMEOUTS = {"locate": 10.0, "service": 25.0, "scheduler": 25.0}
# Where the fleet installs ccc-node, probed on the peer in this order (#1990):
# root-run VPS nodes at /opt or /root, Termux nodes under $HOME.
REMOTE_CHECKOUT_CANDIDATES = ("/opt/ccc-node", "/root/ccc-node", "$HOME/ccc-node")
REMOTE_SCRIPT = "scripts/ccc-node-status.py"
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


# Non-Telegram frontends write the same body-free ``health.json`` schema under
# ``<bridge home>/.ccc-<name>/`` (#2771). A file older than this is stale —
# the same bound ccc-doctor uses for hande bot health.
FRONTEND_HEALTH_DIRS = {"matrix": ".ccc-matrix", "hande": ".ccc-hande"}
FRONTEND_HEALTH_STALE_SECONDS = 300
_FRONTEND_HEALTH_READ_LIMIT = 1 << 18
_HEALTHY_STATES = {"available", "healthy"}


def _declared_frontends(env: Mapping[str, str]) -> set[str] | None:
    """Operator-owned frontend list (#2342), or None when not declared.

    Same contract as ccc-doctor's ``frontends()``: ``CCC_FRONTENDS`` (comma/
    space separated) wins over ``$HOME/.claude/frontends`` (one name per line,
    ``#`` comments). None keeps the legacy Telegram-bridge assumption.
    """

    text = str(env.get("CCC_FRONTENDS", "")).strip()
    if not text:
        home = str(env.get("HOME", "")).strip()
        if not home:
            return None
        try:
            text = (Path(home) / ".claude" / "frontends").read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
    names: set[str] = set()
    for line in text.splitlines():
        names.update(re.split(r"[\s,]+", line.split("#", 1)[0].strip()))
    names.discard("")
    return names


def _frontend_health(name: str, home: Path, now: float) -> dict[str, Any]:
    """Body-free summary of one frontend's ``health.json``; unknown on failure."""

    directory = FRONTEND_HEALTH_DIRS.get(name)
    if directory is None:
        return {"status": "unknown", "error": "no health source for this frontend"}
    path = home / directory / "health.json"
    try:
        with path.open("rb") as handle:
            raw = handle.read(_FRONTEND_HEALTH_READ_LIMIT + 1)
            mtime = os.fstat(handle.fileno()).st_mtime
    except FileNotFoundError:
        return {"status": "unknown", "error": "health.json missing"}
    except OSError as error:
        return {"status": "unknown", "error": f"unreadable: {error.__class__.__name__}"}
    if len(raw) > _FRONTEND_HEALTH_READ_LIMIT:
        return {"status": "unknown", "error": "health.json exceeds the size bound"}
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"status": "unknown", "error": "unparseable health.json"}
    if not isinstance(document, dict):
        return {"status": "unknown", "error": "unparseable health.json"}

    def state(key: str) -> str:
        block = document.get(key)
        value = block.get("state") if isinstance(block, dict) else None
        return value if isinstance(value, str) else "unknown"

    process = document.get("process")
    pid = process.get("pid") if isinstance(process, dict) else None
    age = max(0, int(now - mtime))
    service_state = state("service")
    return {
        "status": "ok",
        "service": service_state,
        "claude": state("claude"),
        "agent": state("agent"),
        "pid": pid if isinstance(pid, int) else "unknown",
        "age_seconds": age,
        "stale": age > FRONTEND_HEALTH_STALE_SECONDS,
        "healthy": service_state.lower() in _HEALTHY_STATES and age <= FRONTEND_HEALTH_STALE_SECONDS,
    }


def _retired_telegram_service(
    parsed: dict[str, Any] | None,
    frontends: set[str],
    home: Path,
    now: float,
) -> dict[str, Any]:
    """Service verdict for a node whose frontend list retires Telegram (#2771).

    The Telegram probe still runs: a retired bridge that is alive again is
    drift (the same rule as ccc-doctor's ``retired_bridge_status_verdict``).
    Health comes from the declared frontends' own ``health.json``.
    """

    service: dict[str, Any] = dict(parsed or {})
    service["telegram"] = "retired (frontend list)"
    if parsed is not None and str(parsed.get("process", "")).lower().startswith("alive"):
        service["telegram_drift"] = "retired but running"
    declared = sorted(frontends - {"telegram"})
    blocks = {name: _frontend_health(name, home, now) for name in declared}
    service["frontends"] = blocks
    if any(block.get("status") == "ok" and block.get("healthy") is False for block in blocks.values()):
        service["healthy"] = False
    elif blocks and all(block.get("healthy") is True for block in blocks.values()):
        service["healthy"] = True
    else:
        # No declared frontend, or one without a readable health source:
        # report unknown rather than fabricate a verdict (#1694).
        service["healthy"] = "unknown"
    return service


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
    bridge_home = project_path or default_project
    section, stdout = _run_section(
        "service",
        _collector_cmd("bridge_status", repo / "bridge" / "start.sh", environment)
        + ["--path", bridge_home, "--status"],
        runner,
    )
    service: dict[str, Any] = {**section}
    parsed = _parse_bridge_status(stdout) if section["status"] == "ok" else None
    if parsed is not None:
        service.update(parsed)
    else:
        partial = True
    frontends = _declared_frontends(environment)
    if frontends is not None and "telegram" not in frontends:
        service.update(
            _retired_telegram_service(parsed, frontends, Path(bridge_home), _dt.datetime.now().timestamp())
        )
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


def remote_command(remote_path: str | None = None) -> str:
    """Shell snippet run on the peer: locate ccc-node-status.py, then exec it.

    Nodes install ccc-node in different places (/opt/ccc-node on root-run
    VPS nodes, $HOME/ccc-node on Termux), so a single fixed remote path can
    never serve a mixed fleet (#1990). ``remote_path`` (from
    ``CCC_NODE_STATUS_REMOTE_PATH``) is tried first when given; then the fleet
    checkout candidates in order. ``$HOME`` is left for the peer's shell to
    expand. No CLI flags are passed: the script without ``--node`` reports
    the node it runs on.
    """

    candidates: list[str] = []
    if remote_path:
        candidates.append(shlex.quote(remote_path))
    for root in REMOTE_CHECKOUT_CANDIDATES:
        if root.startswith("$HOME/"):
            candidates.append(f'"$HOME"/{shlex.quote(root[len("$HOME/"):])}/{REMOTE_SCRIPT}')
        else:
            candidates.append(shlex.quote(f"{root}/{REMOTE_SCRIPT}"))
    searched = ", ".join(REMOTE_CHECKOUT_CANDIDATES)
    return (
        f"for s in {' '.join(candidates)}; do "
        f'[ -f "$s" ] && exec python3 "$s"; done; '
        f"echo {shlex.quote(f'ccc-node-status: {REMOTE_SCRIPT} not found under {searched}')} >&2; exit 127"
    )


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
    remote_path = str(environment.get("CCC_NODE_STATUS_REMOTE_PATH", "")).strip() or None
    ssh_cmd = str(environment.get("CCC_NODE_STATUS_SSH", "")).strip()
    cmd = [
        *(ssh_cmd.split() or ["ssh"]),
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "--",
        node,
        remote_command(remote_path),
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
