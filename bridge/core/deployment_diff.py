"""Pre-deployment diff behind the ``deployment_diff`` tool/CLI (#1694 item 4).

One read-only call aggregates what a deployment pre-check scans for, by
*reusing* this repo's existing sources instead of reimplementing them:

- ``checkout`` — the serving bridge checkout via ``scripts/ccc-bridge-locate.sh
  --json`` (same source as ``node_status``),
- ``target`` — ``git ls-remote origin main`` (no fetch needed),
- ``history`` — bounded ``git fetch`` (the ``skills/self-update/check.sh``
  precedent treats fetch as read-only drift detection) then ahead/behind
  counts and dirty-file count,
- ``installed`` — the last-installed marker
  (``<claude_dir>/state/self-update.installed-sha``) plus harness drift rows
  from ``scripts/ccc_doctor.py --json`` — the canonical installed-state
  comparison per the #1033 phantom-drift lesson (check.sh delegates here and
  so do we; commit distance alone never proves harness currency),
- ``dependencies`` — which dependency files
  (``bridge/requirements*.txt``, ``pyproject.toml``) changed between HEAD and
  the target, plus the serving venv's interpreter version,
- ``recovery`` — the latest ``<claude_dir>/backups/ccc-node-setup-*.tar.gz``
  snapshot (path, size, age).

stdlib-only. Every section carries ``status: ok|unknown`` and its observation
latency; a failed collector becomes section-scoped ``unknown`` — never a
fabricated value. The aggregate ``deployment`` verdict is an informational
snapshot of restart/setup reasons; it never substitutes for the self-update
approval flow or a live restart preflight.
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

LOCATE_TIMEOUT = 10.0
GIT_TIMEOUT = 30.0
FETCH_TIMEOUT = 60.0
DOCTOR_TIMEOUT = 45.0
_DRIFT_STATUSES = {"drifted", "missing"}
_DRIFT_ITEM_CAP = 20
_BACKUP_PATTERN = re.compile(r"^ccc-node-setup-\d{8}-\d{6}\.tar\.gz$")
_DEP_FILES = (
    "bridge/requirements.txt",
    "bridge/requirements.lock.txt",
    "bridge/requirements-voice.txt",
    "pyproject.toml",
)

RUNNER = Callable[[list[str], float], "subprocess.CompletedProcess[bytes]"]


class DeploymentDiffError(ValueError):
    """A structured deployment-diff failure (CLI/ToolError boundary)."""

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


def _claude_dir(env: Mapping[str, str] | None) -> Path:
    environment = os.environ if env is None else env
    configured = str(environment.get("CCC_CLAUDE_DIR", "")).strip()
    if configured:
        return Path(configured).expanduser()
    home = str(environment.get("CCC_SKILL_LOOKUP_HOME", "")).strip()
    return (Path(home).expanduser() if home else Path.home()) / ".claude"


def _marker_path(env: Mapping[str, str] | None) -> Path:
    environment = os.environ if env is None else env
    override = str(environment.get("CCC_DEPLOYMENT_DIFF_MARKER_FILE", "")).strip()
    if override:
        return Path(override).expanduser()
    # NOTE: deliberately does NOT honor CCC_STATE_DIR — inside a bridge session
    # that variable points at the memory-audience state dir, while the
    # self-update marker lives under the harness home (cron writes it there
    # with a clean environment).
    return _claude_dir(env) / "state" / "self-update.installed-sha"


def _cmd_prefix(env: Mapping[str, str] | None, var: str, default: list[str]) -> list[str]:
    environment = os.environ if env is None else env
    override = str(environment.get(var, "")).strip()
    if override:
        return override.split()
    return list(default)


def _fetch_enabled(env: Mapping[str, str] | None) -> bool:
    environment = os.environ if env is None else env
    return str(environment.get("CCC_DEPLOYMENT_DIFF_FETCH", "1")).strip() != "0"


def _now_utc() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _age_hours(timestamp: float) -> float:
    return round(max(0.0, (_dt.datetime.now(_dt.timezone.utc).timestamp() - timestamp) / 3600), 1)


def _default_runner(cmd: list[str], timeout: float, cwd: Path | None) -> "subprocess.CompletedProcess[bytes]":
    return subprocess.run(cmd, capture_output=True, timeout=timeout, cwd=None if cwd is None else str(cwd))


def _run(
    cmd: list[str],
    timeout: float,
    runner: RUNNER | None,
    cwd: Path | None = None,
) -> tuple["subprocess.CompletedProcess[bytes] | None", int, dict[str, Any] | None]:
    """Run one collector command; return (completed, latency_ms, failure)."""

    started = _dt.datetime.now(_dt.timezone.utc)
    try:
        completed = (
            runner(cmd, timeout) if runner is not None else _default_runner(cmd, timeout, cwd)
        )
    except subprocess.TimeoutExpired:
        return None, 0, {"error": "timeout"}
    except OSError as error:
        return None, 0, {"error": f"unavailable: {error}"}
    latency_ms = int((_dt.datetime.now(_dt.timezone.utc) - started).total_seconds() * 1000)
    if completed.returncode != 0:
        # completed is returned even on failure: ccc_doctor.py exits non-zero
        # whenever it finds correctable drift while emitting valid JSON on
        # stdout — callers that need the payload on failure (the doctor
        # section) rely on it; the rest branch on ``failure`` only.
        return completed, latency_ms, {
            "error": f"exit {completed.returncode}",
            "latency_ms": latency_ms,
            "stderr_tail": completed.stderr.decode("utf-8", "replace")[-300:],
        }
    return completed, latency_ms, None


def _checkout_section(
    env: Mapping[str, str] | None,
    runner: RUNNER | None,
    repo_root: Path,
) -> dict[str, Any]:
    default = ["bash", str(repo_root / "scripts" / "ccc-bridge-locate.sh"), "--json"]
    cmd = _cmd_prefix(env, "CCC_DEPLOYMENT_DIFF_LOCATE", default)
    completed, latency_ms, failure = _run(cmd, LOCATE_TIMEOUT, runner)
    if failure is not None or completed is None:
        return {"status": "unknown", **(failure or {})}
    try:
        document = json.loads(completed.stdout.decode("utf-8", "replace"))
        bridges = document["bridges"]
        serving = bridges[0]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        return {"status": "unknown", "error": "locate output shape invalid", "latency_ms": latency_ms}
    return {
        "status": "ok",
        "latency_ms": latency_ms,
        "running": bool(document.get("running")),
        "pid": serving.get("pid"),
        "checkout": serving.get("checkout"),
        "head": serving.get("head"),
        "branch": serving.get("branch"),
        "dirty": serving.get("dirty"),
    }


def _run_git(
    env: Mapping[str, str] | None,
    runner: RUNNER | None,
    repo_root: Path,
    args: list[str],
    timeout: float,
) -> tuple[str, int, dict[str, Any] | None]:
    cmd = _cmd_prefix(env, "CCC_DEPLOYMENT_DIFF_GIT", ["git"]) + args
    completed, latency_ms, failure = _run(cmd, timeout, runner, cwd=repo_root)
    if failure is not None or completed is None:
        return "", latency_ms, failure if failure is not None else {"error": "no output"}
    return completed.stdout.decode("utf-8", "replace"), latency_ms, None


def _target_section(
    env: Mapping[str, str] | None,
    runner: RUNNER | None,
    repo_root: Path,
) -> tuple[dict[str, Any], str | None]:
    """``git ls-remote origin main`` — the target SHA without a fetch."""

    target_out, latency, failure = _run_git(env, runner, repo_root, ["ls-remote", "origin", "main"], GIT_TIMEOUT)
    if failure is not None:
        failure = dict(failure)
        failure["latency_ms"] = latency
        return {"status": "unknown", **failure}, None
    target_sha = target_out.split()[0] if target_out.split() else ""
    return {"status": "ok", "latency_ms": latency, "branch": "main", "sha": target_sha or None}, target_sha or None


def _history_section(
    env: Mapping[str, str] | None,
    runner: RUNNER | None,
    repo_root: Path,
) -> tuple[dict[str, Any], str | None]:
    head_out, latency, failure = _run_git(env, runner, repo_root, ["rev-parse", "HEAD"], GIT_TIMEOUT)
    if failure is not None:
        failure = dict(failure)
        failure["latency_ms"] = latency
        return {"status": "unknown", **failure}, None
    head_sha = head_out.strip()

    fetched = False
    if _fetch_enabled(env):
        _fetch_out, _latency, fetch_failure = _run_git(
            env, runner, repo_root, ["fetch", "origin", "--quiet"], FETCH_TIMEOUT
        )
        fetched = fetch_failure is None

    behind: Any = None
    ahead: Any = None
    if fetched:
        behind_out, _, failure = _run_git(
            env, runner, repo_root, ["rev-list", "--count", f"{head_sha}..origin/main"], GIT_TIMEOUT
        )
        if failure is None:
            behind = int(behind_out.strip()) if behind_out.strip().isdigit() else None
        ahead_out, _, failure = _run_git(
            env, runner, repo_root, ["rev-list", "--count", f"origin/main..{head_sha}"], GIT_TIMEOUT
        )
        if failure is None:
            ahead = int(ahead_out.strip()) if ahead_out.strip().isdigit() else None

    status_out, _, _ = _run_git(env, runner, repo_root, ["status", "--porcelain"], GIT_TIMEOUT)
    dirty = len([line for line in status_out.splitlines() if line.strip()]) if status_out else None

    section: dict[str, Any] = {
        "status": "ok",
        "latency_ms": None,
        "head": head_sha or None,
        "dirty_files": dirty,
        "fetched": fetched,
        "behind": behind,
        "ahead": ahead,
    }
    if behind is None:
        section["behind_note"] = "unknown (fetch disabled or failed)"
    return section, head_sha or None


def _deps_section(
    env: Mapping[str, str] | None,
    runner: RUNNER | None,
    repo_root: Path,
    history: dict[str, Any],
) -> dict[str, Any]:
    behind = history.get("behind") if history["status"] == "ok" else None
    section: dict[str, Any] = {"status": "ok", "changed_files": [], "reason": None}
    if behind is None:
        section["reason"] = "behind count unknown — diff skipped"
    elif behind == 0:
        section["reason"] = "checkout is at origin/main"
    else:
        head_sha = str(history.get("head") or "HEAD")
        dep_args = ["diff", "--name-only", f"{head_sha}..origin/main", "--", *_DEP_FILES]
        deps_out, _, failure = _run_git(env, runner, repo_root, dep_args, GIT_TIMEOUT)
        if failure is not None:
            return {"status": "unknown", **failure}
        section["changed_files"] = [line for line in deps_out.splitlines() if line.strip()]
        section["reason"] = f"{behind} commit(s) between HEAD and origin/main"
    venv_cfg = repo_root / "bridge" / "venv" / "pyvenv.cfg"
    if venv_cfg.exists():
        try:
            for line in venv_cfg.read_text(encoding="utf-8").splitlines():
                if line.startswith("version"):
                    section["venv_python"] = line.split("=", 1)[1].strip()
                    break
        except OSError:
            pass
    return section


def _git_sections(
    env: Mapping[str, str] | None,
    runner: RUNNER | None,
    repo_root: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    if not (repo_root / ".git").exists():
        unknown = {"status": "unknown", "error": f"no git checkout at {repo_root}"}
        return unknown, dict(unknown), {"status": "unknown", "error": "no git checkout"}

    target_section, _target_sha = _target_section(env, runner, repo_root)
    if target_section["status"] != "ok":
        failed = {"status": "unknown", "error": "ls-remote failed"}
        return target_section, failed, dict(failed)

    history_section, _head_sha = _history_section(env, runner, repo_root)
    deps_section = _deps_section(env, runner, repo_root, history_section)
    return target_section, history_section, deps_section


def _installed_section(
    env: Mapping[str, str] | None,
    runner: RUNNER | None,
    repo_root: Path,
) -> dict[str, Any]:
    marker_path = _marker_path(env)
    section: dict[str, Any] = {"status": "ok", "marker": {"path": str(marker_path)}}
    try:
        stat_result = marker_path.stat()
        content = marker_path.read_text(encoding="utf-8").strip()
        section["marker"].update(
            {
                "present": True,
                "sha": content or None,
                "age_hours": _age_hours(stat_result.st_mtime),
            }
        )
    except FileNotFoundError:
        section["marker"]["present"] = False
    except OSError as error:
        section["marker"]["present"] = False
        section["marker"]["error"] = f"unreadable: {error}"

    default = ["python3", str(repo_root / "scripts" / "ccc_doctor.py"), "--json"]
    cmd = _cmd_prefix(env, "CCC_DEPLOYMENT_DIFF_DOCTOR", default)
    completed, latency_ms, failure = _run(cmd, DOCTOR_TIMEOUT, runner, cwd=repo_root)
    doctor: dict[str, Any]
    # ccc_doctor.py exits non-zero whenever it classifies anything as 교정가능 —
    # which is precisely the drift case (same trap check.sh documents). The JSON
    # on stdout is valid data at any exit code, so parse it regardless and keep
    # the exit code as evidence; only timeout/OSError/unparseable is unknown.
    if completed is None:
        doctor = {"status": "unknown", **(failure or {})}
    else:
        try:
            rows = json.loads(completed.stdout.decode("utf-8", "replace"))["rows"]
            if not isinstance(rows, list):
                raise TypeError("rows not a list")
        except (json.JSONDecodeError, KeyError, TypeError):
            doctor = {
                "status": "unknown",
                "error": f"doctor output unparseable (exit {completed.returncode})",
                "latency_ms": latency_ms,
                "stderr_tail": completed.stderr.decode("utf-8", "replace")[-300:],
            }
        else:
            counts: dict[str, int] = {}
            drift_items: list[str] = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                status = str(row.get("status") or "unknown")
                counts[status] = counts.get(status, 0) + 1
                if status in _DRIFT_STATUSES and len(drift_items) < _DRIFT_ITEM_CAP:
                    drift_items.append(str(row.get("item") or "unknown-item"))
            doctor = {
                "status": "ok",
                "latency_ms": latency_ms,
                "exit_code": completed.returncode,
                "rows_total": len(rows),
                "status_counts": counts,
                "drift_items": drift_items,
                "drift_total": counts.get("drifted", 0) + counts.get("missing", 0),
            }
    section["doctor"] = doctor
    if doctor["status"] != "ok" and section["marker"].get("present") is not True:
        section["status"] = "unknown"
    return section


def _recovery_section(env: Mapping[str, str] | None) -> dict[str, Any]:
    backups_dir = _claude_dir(env) / "backups"
    section: dict[str, Any] = {"status": "ok", "backups_dir": str(backups_dir)}
    try:
        candidates = sorted(
            (entry for entry in backups_dir.iterdir() if _BACKUP_PATTERN.match(entry.name)),
            key=lambda entry: entry.stat().st_mtime,
            reverse=True,
        )
    except FileNotFoundError:
        section.update({"latest": None, "total": 0, "error": "backups dir missing"})
        return section
    except OSError as error:
        return {"status": "unknown", "backups_dir": str(backups_dir), "error": f"unreadable: {error}"}
    section["total"] = len(candidates)
    if not candidates:
        section["latest"] = None
        return section
    latest = candidates[0]
    try:
        size = latest.stat().st_size
        mtime = latest.stat().st_mtime
    except OSError as error:
        section["latest"] = {"name": latest.name, "error": f"unreadable: {error}"}
        return section
    section["latest"] = {
        "name": latest.name,
        "path": str(latest),
        "size_bytes": size,
        "age_hours": _age_hours(mtime),
    }
    return section


def _deployment_verdict(
    checkout: dict[str, Any],
    history: dict[str, Any],
    installed: dict[str, Any],
    repo_root: Path,
) -> dict[str, Any]:
    reasons: list[str] = []
    repo_root_str = str(repo_root)
    if history["status"] == "ok":
        if history.get("behind") is not None and history["behind"] > 0:
            reasons.append(f"checkout_behind:{history['behind']}")
        if history.get("dirty_files"):
            reasons.append(f"checkout_dirty:{history['dirty_files']}")
    else:
        reasons.append("history_unknown")
    if checkout["status"] == "ok":
        if not checkout.get("running"):
            reasons.append("bridge_not_running")
        serving_head = str(checkout.get("head") or "").lower()
        serving_path = str(checkout.get("checkout") or "")
        local_head = str(history.get("head") or "").lower() if history["status"] == "ok" else ""
        if serving_head and local_head and serving_path == repo_root_str and serving_head != local_head:
            reasons.append("bridge_running_old_code")
    else:
        reasons.append("checkout_unknown")
    if installed["status"] == "ok":
        drift_total = installed.get("doctor", {}).get("drift_total")
        if isinstance(drift_total, int) and drift_total > 0:
            reasons.append(f"installed_drift:{drift_total}")
        marker = installed.get("marker", {})
        marker_sha = str(marker.get("sha") or "").lower()
        local_head = str(history.get("head") or "").lower() if history["status"] == "ok" else ""
        if marker.get("present") and local_head and marker_sha != local_head:
            reasons.append("installed_marker_differs")
        if not marker.get("present"):
            reasons.append("installed_marker_missing")
    else:
        reasons.append("installed_unknown")
    return {
        "verdict": "restart_recommended" if reasons else "up_to_date",
        "reasons": reasons,
        "informational_only": True,
    }


def collect(
    *,
    env: Mapping[str, str] | None = None,
    runner: RUNNER | None = None,
) -> dict[str, Any]:
    """Collect the pre-deployment diff; collector failures become unknown."""

    try:
        from telegram_bot.core.skill_lookup import policy_denial
    except ImportError:  # pragma: no cover - worktree aliasing only
        from skill_lookup import policy_denial

    denial = policy_denial(env)
    if denial is not None:
        raise DeploymentDiffError("policy_denied", "deployment diff denied by node policy", reason=denial)

    repo_root = _repo_root(env)
    checkout = _checkout_section(env, runner, repo_root)
    target, history, dependencies = _git_sections(env, runner, repo_root)
    installed = _installed_section(env, runner, repo_root)
    recovery = _recovery_section(env)

    sections = {
        "checkout": checkout,
        "target": target,
        "history": history,
        "installed": installed,
        "dependencies": dependencies,
        "recovery": recovery,
    }
    partial = any(section["status"] != "ok" for section in sections.values())
    return {
        "observed_at": _now_utc(),
        "node": socket.gethostname(),
        "repo_root": str(repo_root),
        "status": "unknown" if partial else "ok",
        "sections": sections,
        "deployment": _deployment_verdict(checkout, history, installed, repo_root),
    }
