#!/usr/bin/env python3
"""main-model-switch: switch the bridges' main Claude model in one place (#2557).

Replaces the node-local ``apply.sh`` scripts that each model switch used to
hand-roll. One of those (2026-10-27, #2571) moved the Matrix env file into
``retired/`` while ``ccc-hande-bridge.service`` -- whose drop-ins
``install-hande-bridge.sh`` copies from Matrix -- still required it, and the
hande bridge stayed down for 14.7 h on its next restart.

What it changes (only paths are ever printed, never env file contents):

* Targets: every ``ccc-*-bridge.service.d/`` under ``--systemd-dir`` holding a
  ``zzzzzzzzzzzzzz-main-*.conf`` drop-in; no channel list is hard-coded.
* For each env file in ``--env-dir`` such a drop-in references (the one that
  sets ``ANTHROPIC_MODEL``): a new ``<channel>-<slug>.env`` copy with only
  ``ANTHROPIC_MODEL``, ``CLAUDE_CODE_EFFORT_LEVEL`` and ``CCC_MODEL_LABEL``
  substituted (0600). EVERY main drop-in that pointed at the old file -- the
  hande clone of the Matrix drop-in included -- is rewritten to
  ``zzzzzzzzzzzzzz-main-<slug>.conf`` pointing at the new file; the old
  drop-in moves to ``<backup>/retired/``. Other drop-ins are never touched.
* ``"model"`` in ``<claude-dir>/settings.json`` / ``settings.local.json``
  (only when the key is already there), and per-chat ``model`` (+ ``effort``
  when pinned) in each ``<bridge-data-dir>/sessions.json`` entry whose model
  starts with ``claude-`` and whose provider is claude (or unset).
* ``systemctl daemon-reload``; an old env file moves to ``<backup>/retired/``
  only when ``env-file-guard.py users <old>`` exits 0, otherwise it is kept and
  reported. ``env-file-guard.py check`` must pass before (else nothing is
  changed) and after (else everything is rolled back, exit 1).

Nothing is restarted by default: the restart commands for the active target
units are printed. ``--restart`` waits for ``bridge-idle-gate.py`` to report
idle (bounded by ``--idle-timeout``), restarts, and verifies
``/proc/<MainPID>/environ`` ``ANTHROPIC_MODEL``. A failed restart is reported
(exit 1) together with the rollback command; it is not rolled back
automatically.

Every touched file is copied to ``<backup-root>/main-<slug>-<node>-<ts>/files``
and listed in ``manifest.json`` (paths only); ``--rollback <dir>`` restores
them, reloads systemd and re-runs the guard check.

The A2A worker model (``A2A_CLAUDE_MODEL``, ``/etc/default/a2a-*``) is out of
scope and never touched.

Usage:
  main-model-switch.py --model <id> --effort <level> --label <text> --slug <slug> --dry-run
  main-model-switch.py --model <id> --effort <level> --label <text> --slug <slug> [--restart]
  main-model-switch.py --rollback <backup-dir>

``SYSTEMCTL`` overrides the systemctl binary (tests use a fake one).
Exit codes: 0 done (or nothing to do), 1 failed / rolled back / restart not
verified, 2 usage or preflight error (nothing changed).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
GUARD = HERE / "env-file-guard.py"
IDLE_GATE = HERE / "bridge-idle-gate.py"
SCHEMA = "ccc.main-model-switch.v1"
MAIN_PREFIX = "zzzzzzzzzzzzzz-main-"
UNIT_DIR_GLOB = "ccc-*-bridge.service.d"
MODEL_KEY = "ANTHROPIC_MODEL"
EFFORT_KEY = "CLAUDE_CODE_EFFORT_LEVEL"
LABEL_KEY = "CCC_MODEL_LABEL"
DEFAULT_DATA_DIRS = ("~/.telegram_bot", "~/.ccc-matrix", "~/.ccc-hande")
SYSTEMCTL_TIMEOUT_S = 60.0

EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 2

_SLUG = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@\[\]-]{0,127}$")
_EFFORT = re.compile(r"^[a-z]{2,16}$")
_LABEL_FORBIDDEN = re.compile(r"[\x00-\x1f\x7f\"'\\$`]")
_ASSIGN = re.compile(r"^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_]*)(\s*=\s*)(.*)$")
_ENVFILE = re.compile(r"^(\s*EnvironmentFile\s*=\s*)(-?)(\S.*?)\s*$")


class SwitchError(RuntimeError):
    """A precondition or step failed; the message is path-only."""


def say(message: str) -> None:
    print(f"main-model-switch: {message}", flush=True)


def warn(message: str) -> None:
    print(f"main-model-switch: WARN {message}", file=sys.stderr, flush=True)


# ---- systemctl / helper scripts ---------------------------------------------------


def systemctl(*args: str) -> subprocess.CompletedProcess:
    cmd = [os.environ.get("SYSTEMCTL") or "systemctl", *args]
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=SYSTEMCTL_TIMEOUT_S, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(cmd, 255, "", str(exc))


def daemon_reload() -> bool:
    proc = systemctl("daemon-reload")
    if proc.returncode != 0:
        warn(f"systemctl daemon-reload failed (exit {proc.returncode})")
    return proc.returncode == 0


def run_guard(*args: str) -> subprocess.CompletedProcess:
    """Run the repo's env-file-guard.py; its output is path-only by contract."""
    cmd = [sys.executable, str(GUARD), *args]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(cmd, 2, "", str(exc))


def relay(proc: subprocess.CompletedProcess, prefix: str = "  guard: ") -> None:
    for line in (proc.stdout + proc.stderr).splitlines():
        if line.strip():
            print(f"{prefix}{line}", flush=True)


# ---- file helpers -----------------------------------------------------------------


def atomic_write(path: Path, text: str, mode: int, owner: tuple[int, int] | None = None) -> None:
    """Write ``text`` to ``path`` via a same-directory temp file and rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), mode)
            if owner is not None and os.geteuid() == 0:
                os.fchown(handle.fileno(), *owner)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def file_meta(path: Path) -> tuple[int, tuple[int, int]]:
    st = path.stat()
    return st.st_mode & 0o7777, (st.st_uid, st.st_gid)


def dump_json(data: object) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


# ---- env files and drop-ins -------------------------------------------------------


def _quote(value: str, old_value: str) -> str:
    style = old_value[:1] if old_value[:1] in {'"', "'"} else ""
    if not style and re.search(r"[\s#;]", value):
        style = '"'
    return f"{style}{value}{style}"


def rewrite_env(text: str, values: dict[str, str]) -> str:
    """Substitute the switch keys in env ``text``; other lines stay byte-identical."""
    lines = text.splitlines(keepends=True)
    seen: set[str] = set()
    for index, line in enumerate(lines):
        body = line.rstrip("\r\n")
        match = _ASSIGN.match(body)
        if not match or match.group(2) not in values:
            continue
        prefix, key, equals, old_value = match.groups()
        lines[index] = (
            f"{prefix}{key}{equals}{_quote(values[key], old_value.strip())}{line[len(body):]}"
        )
        seen.add(key)
    if MODEL_KEY not in seen:
        raise SwitchError("env file has no ANTHROPIC_MODEL line")
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    for key in values:
        if key not in seen:
            lines.append(f"{key}={_quote(values[key], '')}\n")
    return "".join(lines)


def env_sets_model(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return any(
        (m := _ASSIGN.match(line)) and m.group(2) == MODEL_KEY for line in text.splitlines()
    )


def new_env_path(old: Path, slug: str, env_dir: Path) -> Path:
    channel = old.name.split("-", 1)[0] if "-" in old.name else old.stem
    return env_dir / f"{channel}-{slug}.env"


@dataclass
class MainDropIn:
    unit: str
    path: Path
    text: str
    model_env: Path


def _env_refs(text: str, env_dir: Path) -> list[Path]:
    refs = []
    for line in text.splitlines():
        match = _ENVFILE.match(line)
        if match:
            ref = Path(os.path.normpath(match.group(3)))
            if ref.parent == env_dir:
                refs.append(ref)
    return refs


def discover(systemd_dir: Path, env_dir: Path) -> list[MainDropIn]:
    found = []
    for unit_dir in sorted(systemd_dir.glob(UNIT_DIR_GLOB)):
        mains = sorted(unit_dir.glob(f"{MAIN_PREFIX}*.conf"))
        if not mains:
            continue
        if len(mains) > 1:
            names = ", ".join(p.name for p in mains)
            raise SwitchError(f"{unit_dir}: more than one main drop-in ({names})")
        text = mains[0].read_text(encoding="utf-8")
        models = [ref for ref in _env_refs(text, env_dir) if env_sets_model(ref)]
        if not models:
            warn(f"{mains[0]}: no EnvironmentFile in {env_dir} sets {MODEL_KEY}; skipped")
            continue
        if len(set(models)) > 1:
            raise SwitchError(f"{mains[0]}: more than one model env file referenced")
        unit = unit_dir.name[: -len(".d")]
        found.append(MainDropIn(unit, mains[0], text, models[0]))
    return found


def render_dropin(old_text: str, mapping: dict[Path, Path], header: list[str]) -> str:
    body = []
    for line in old_text.splitlines():
        if line.lstrip().startswith(("#", ";")):
            continue
        match = _ENVFILE.match(line)
        if match:
            ref = Path(os.path.normpath(match.group(3)))
            if ref in mapping:
                line = f"{match.group(1)}{match.group(2)}{mapping[ref]}"
        body.append(line)
    while body and not body[0].strip():
        body.pop(0)
    return "\n".join([*header, *body]) + "\n"


# ---- planning ---------------------------------------------------------------------


@dataclass
class Write:
    path: Path
    text: str
    mode: int
    owner: tuple[int, int] | None
    note: str


@dataclass
class Plan:
    units: list[str] = field(default_factory=list)
    writes: list[Write] = field(default_factory=list)
    retire_dropins: list[Path] = field(default_factory=list)
    old_envs: list[Path] = field(default_factory=list)

    def empty(self) -> bool:
        return not (self.writes or self.retire_dropins or self.old_envs)


def _plan_envs(plan: Plan, mains: list[MainDropIn], args: argparse.Namespace) -> dict:
    values = {MODEL_KEY: args.model, EFFORT_KEY: args.effort, LABEL_KEY: args.label}
    mapping: dict[Path, Path] = {}
    for old in sorted({m.model_env for m in mains}):
        new = new_env_path(old, args.slug, args.env_dir)
        if new in mapping.values():
            raise SwitchError(f"two env files would both become {new}")
        mapping[old] = new
        old_text = old.read_text(encoding="utf-8")
        new_text = rewrite_env(old_text, values)
        _, owner = file_meta(old)
        if new != old:
            plan.old_envs.append(old)
            note = f"env {old.name} -> {new.name}" + (" (replaces existing)" if new.exists() else "")
        elif new_text != old_text:
            note = f"env {old.name}: update in place"
        else:
            continue
        plan.writes.append(Write(new, new_text, 0o600, owner, note))
    return mapping


def _dropin_header(args: argparse.Namespace, backup_dir: Path) -> list[str]:
    return [
        "# ccc-node #2557: main model drop-in written by scripts/main-model-switch.py",
        f"# model={args.model} effort={args.effort} label={args.label} slug={args.slug}",
        f"# backup: {backup_dir}",
        f"# rollback: python3 {Path(__file__).resolve()} --rollback {backup_dir}",
    ]


def _plan_dropins(
    plan: Plan, mains: list[MainDropIn], mapping: dict, args: argparse.Namespace, backup_dir: Path
) -> None:
    header = _dropin_header(args, backup_dir)
    for main in mains:
        plan.units.append(main.unit)
        target = main.path.parent / f"{MAIN_PREFIX}{args.slug}.conf"
        if main.path == target and mapping[main.model_env] == main.model_env:
            say(f"{main.unit}: already on {target.name}")
            continue
        _, owner = file_meta(main.path)
        note = f"{main.unit}: {main.path.name} -> {target.name} ({mapping[main.model_env].name})"
        plan.writes.append(
            Write(target, render_dropin(main.text, mapping, header), 0o644, owner, note)
        )
        if main.path != target:
            plan.retire_dropins.append(main.path)


def _plan_settings(plan: Plan, args: argparse.Namespace) -> None:
    for name in ("settings.json", "settings.local.json"):
        path = args.claude_dir / name
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            warn(f"{path}: not readable JSON; skipped")
            continue
        if not isinstance(data, dict) or "model" not in data or data["model"] == args.model:
            continue
        data["model"] = args.model
        mode, owner = file_meta(path)
        plan.writes.append(Write(path, dump_json(data), mode, owner, f"{path}: model"))


def _update_sessions(data: dict, model: str, effort: str) -> int:
    changed = 0
    for entry in data.values():
        if not isinstance(entry, dict):
            continue
        current = entry.get("model")
        if not (isinstance(current, str) and current.startswith("claude-")):
            continue
        if entry.get("provider") not in (None, "claude"):
            continue
        before = dict(entry)
        entry["model"] = model
        if "effort" in entry:
            entry["effort"] = effort
        changed += entry != before
    return changed


def _plan_sessions(plan: Plan, args: argparse.Namespace) -> None:
    for data_dir in args.bridge_data_dirs:
        path = Path(os.path.expanduser(data_dir)) / "sessions.json"
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            warn(f"{path}: not readable JSON; skipped")
            continue
        if not isinstance(data, dict):
            continue
        changed = _update_sessions(data, args.model, args.effort)
        if changed:
            mode, owner = file_meta(path)
            note = f"{path}: {changed} claude chat(s)"
            plan.writes.append(Write(path, dump_json(data), mode, owner, note))


def build_plan(args: argparse.Namespace, backup_dir: Path) -> Plan:
    plan = Plan()
    mains = discover(args.systemd_dir, args.env_dir)
    if not mains:
        raise SwitchError(f"no {UNIT_DIR_GLOB}/{MAIN_PREFIX}*.conf under {args.systemd_dir}")
    mapping = _plan_envs(plan, mains, args)
    _plan_dropins(plan, mains, mapping, args, backup_dir)
    _plan_settings(plan, args)
    _plan_sessions(plan, args)
    return plan


# ---- backup manifest and rollback -------------------------------------------------


class Manifest:
    def __init__(self, backup_dir: Path, meta: dict):
        self.dir = backup_dir
        self.data = {"schema": SCHEMA, **meta, "status": "in-progress", "entries": []}

    @classmethod
    def load(cls, backup_dir: Path) -> "Manifest":
        manifest = cls(backup_dir, {})
        manifest.data = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
        if manifest.data.get("schema") != SCHEMA:
            raise SwitchError(f"{backup_dir}/manifest.json: unknown schema")
        return manifest

    def record(self, path: Path, action: str) -> dict:
        entry: dict = {"path": str(path), "action": action, "backup": None}
        if path.exists():
            rel = Path("files") / str(path).lstrip("/")
            (self.dir / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, self.dir / rel)
            mode, owner = file_meta(path)
            entry.update(backup=str(rel), mode=mode, uid=owner[0], gid=owner[1])
        self.data["entries"].append(entry)
        return entry

    def save(self, status: str | None = None) -> None:
        if status:
            self.data["status"] = status
        atomic_write(self.dir / "manifest.json", dump_json(self.data), 0o600)

    def retire(self, path: Path, sub: str = "") -> Path:
        dest = self.dir / "retired" / sub / path.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(dest))
        return dest


def restore(manifest: Manifest) -> list[str]:
    """Put every recorded path back the way it was; returns failures (paths)."""
    failed = []
    for entry in reversed(manifest.data.get("entries", [])):
        path = Path(entry["path"])
        try:
            if entry.get("backup"):
                source = (manifest.dir / entry["backup"]).resolve()
                if manifest.dir.resolve() not in source.parents:
                    raise SwitchError(f"backup path escapes {manifest.dir}")
                owner = (entry["uid"], entry["gid"])
                atomic_write(path, source.read_text(encoding="utf-8"), entry["mode"], owner)
            elif path.exists():
                path.unlink()
        except (OSError, SwitchError, KeyError) as exc:
            failed.append(f"{path} ({type(exc).__name__})")
    return failed


def rollback(manifest: Manifest, reason: str) -> int:
    say(f"rolling back from {manifest.dir} ({reason})")
    failed = restore(manifest)
    for item in failed:
        warn(f"could not restore {item}")
    reloaded = daemon_reload()
    check = run_guard("check")
    relay(check)
    manifest.save("rolled-back" if not failed else "rollback-incomplete")
    ok = not failed and reloaded and check.returncode == 0
    say("rollback " + ("complete" if ok else "INCOMPLETE -- inspect the paths above"))
    return EXIT_OK if ok else EXIT_FAIL


# ---- restart ----------------------------------------------------------------------


def unit_main_pid(unit: str) -> tuple[int, Path | None]:
    proc = systemctl("show", unit, "-p", "MainPID", "-p", "Environment", "--value")
    lines = proc.stdout.splitlines()
    try:
        pid = int(lines[0])
    except (IndexError, ValueError):
        return 0, None
    data_dir = None
    for item in " ".join(lines[1:]).split():
        if item.startswith("BOT_DATA_DIR="):
            data_dir = Path(item.split("=", 1)[1])
    return pid, data_dir


def live_model(proc_root: Path, pid: int) -> str | None:
    if pid <= 0:
        return None
    try:
        raw = (proc_root / str(pid) / "environ").read_bytes()
    except OSError:
        return None
    for item in raw.split(b"\0"):
        if item.startswith(MODEL_KEY.encode() + b"="):
            return item.split(b"=", 1)[1].decode("utf-8", "replace")
    return None


def active_units(units: list[str]) -> list[str]:
    return [u for u in units if systemctl("is-active", u).stdout.strip() == "active"]


def wait_idle(unit: str, args: argparse.Namespace) -> bool:
    deadline = time.monotonic() + args.idle_timeout
    while True:
        pid, data_dir = unit_main_pid(unit)
        cmd = [sys.executable, str(IDLE_GATE), "--main-pid", str(pid), "--proc", str(args.proc)]
        if data_dir is not None:
            cmd += ["--health", str(data_dir / "health.json")]
        gate = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
        verdict = gate.stdout.strip().splitlines()[:1]
        if gate.returncode == 0:
            return True
        if time.monotonic() >= deadline:
            say(f"{unit}: not idle within {args.idle_timeout:g}s ({' '.join(verdict)})")
            return False
        time.sleep(min(args.idle_interval, max(0.0, deadline - time.monotonic())))


def restart_unit(unit: str, args: argparse.Namespace) -> bool:
    if f"/{unit}" in _self_cgroup(args.self_cgroup):
        say(f"{unit}: this process runs inside it -- re-run detached (systemd-run)")
        return False
    if not wait_idle(unit, args):
        return False
    proc = systemctl("restart", unit)
    if proc.returncode != 0:
        say(f"{unit}: systemctl restart failed (exit {proc.returncode})")
        return False
    deadline = time.monotonic() + args.verify_timeout
    seen = None
    while True:
        pid, _ = unit_main_pid(unit)
        seen = live_model(args.proc, pid)
        if seen == args.model or time.monotonic() >= deadline:
            break
        time.sleep(1.0)
    say(f"{unit}: {MODEL_KEY}={seen if seen is not None else '<unreadable>'}")
    return seen == args.model


def _self_cgroup(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def handle_restarts(units: list[str], args: argparse.Namespace, backup_dir: Path | None) -> int:
    pending = []
    for unit in active_units(units):
        pid, _ = unit_main_pid(unit)
        if live_model(args.proc, pid) == args.model:
            say(f"{unit}: already running {args.model}")
        else:
            pending.append(unit)
    if not pending:
        return EXIT_OK
    if not args.restart:
        say("not restarted (owner-approved step; run each only when its idle gate says idle):")
        for unit in pending:
            print(
                f"  python3 {IDLE_GATE} --unit {unit} && systemctl restart {unit}", flush=True
            )
        return EXIT_OK
    failed = [unit for unit in pending if not restart_unit(unit, args)]
    if failed:
        say(f"restart not verified for: {', '.join(failed)}")
        if backup_dir is not None:
            say(f"rollback: python3 {Path(__file__).resolve()} --rollback {backup_dir}")
        return EXIT_FAIL
    say("restart verified for: " + ", ".join(pending))
    return EXIT_OK


# ---- switch -----------------------------------------------------------------------


def _backup_dir(args: argparse.Namespace) -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%S")
    base = args.backup_root / f"main-{args.slug}-{args.node}-{stamp}"
    candidate, n = base, 1
    while candidate.exists():
        n += 1
        candidate = base.with_name(f"{base.name}-{n}")
    return candidate


def print_plan(plan: Plan, args: argparse.Namespace) -> None:
    say(f"target model={args.model} effort={args.effort} label={args.label} slug={args.slug}")
    say(f"units: {', '.join(plan.units)}")
    for write in plan.writes:
        say(f"  write {write.path} -- {write.note}")
    for path in plan.retire_dropins:
        say(f"  retire drop-in {path}")
    for path in plan.old_envs:
        say(f"  retire env {path} (only if env-file-guard users exits 0)")


def retire_old_envs(manifest: Manifest, old_envs: list[Path]) -> None:
    for old in old_envs:
        users = run_guard("users", str(old))
        if users.returncode == 0:
            dest = manifest.retire(old)
            say(f"retired {old} -> {dest}")
            continue
        state = "still referenced" if users.returncode == 1 else "guard could not verify"
        warn(f"kept {old}: {state} (env-file-guard users exit {users.returncode})")
        relay(users)
        for entry in manifest.data["entries"]:
            if entry["path"] == str(old):
                entry["action"] = "kept"


def apply_plan(plan: Plan, args: argparse.Namespace, backup_dir: Path) -> int:
    backup_dir.mkdir(parents=True, mode=0o700)
    meta = {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "node": args.node,
        "slug": args.slug,
        "model": args.model,
        "units": plan.units,
    }
    manifest = Manifest(backup_dir, meta)
    for write in plan.writes:
        manifest.record(write.path, "modified" if write.path.exists() else "created")
    for path in plan.retire_dropins:
        manifest.record(path, "retired")
    for path in plan.old_envs:
        manifest.record(path, "retired")
    manifest.save()
    say(f"backup: {backup_dir}")
    try:
        for write in plan.writes:
            atomic_write(write.path, write.text, write.mode, write.owner)
        for path in plan.retire_dropins:
            manifest.retire(path, path.parent.name)
    except OSError as exc:
        rollback(manifest, f"write failed: {type(exc).__name__}")
        return EXIT_FAIL
    if not daemon_reload():
        rollback(manifest, "daemon-reload failed")
        return EXIT_FAIL
    retire_old_envs(manifest, plan.old_envs)
    check = run_guard("check")
    if check.returncode != 0:
        relay(check)
        manifest.save()
        rollback(manifest, f"env-file-guard check exit {check.returncode}")
        return EXIT_FAIL
    manifest.save("applied")
    say(f"switched; rollback: python3 {Path(__file__).resolve()} --rollback {backup_dir}")
    return EXIT_OK


def switch(args: argparse.Namespace) -> int:
    backup_dir = _backup_dir(args)
    try:
        plan = build_plan(args, backup_dir)
    except (SwitchError, OSError) as exc:
        say(f"cannot plan: {exc}")
        return EXIT_USAGE
    print_plan(plan, args)
    pre = run_guard("check")
    if pre.returncode != 0:
        relay(pre)
        say("env-file-guard check fails BEFORE the switch -- fix that first; nothing changed")
        return EXIT_USAGE
    if args.dry_run:
        say("dry-run: nothing written")
        args.restart = False
        return handle_restarts(plan.units, args, None)
    if plan.empty():
        say(f"already on {args.slug}: nothing to change")
        return handle_restarts(plan.units, args, None)
    result = apply_plan(plan, args, backup_dir)
    if result != EXIT_OK:
        return result
    return handle_restarts(plan.units, args, backup_dir)


def do_rollback(args: argparse.Namespace) -> int:
    try:
        manifest = Manifest.load(args.rollback)
    except (OSError, ValueError, SwitchError) as exc:
        say(f"cannot read manifest in {args.rollback}: {type(exc).__name__}: {exc}")
        return EXIT_USAGE
    if args.dry_run:
        for entry in manifest.data.get("entries", []):
            verb = "restore" if entry.get("backup") else "remove"
            say(f"  would {verb} {entry['path']}")
        return EXIT_OK
    result = rollback(manifest, "requested")
    units = manifest.data.get("units") or []
    if units:
        say("restart the affected units (owner-approved, after the idle gate): " + ", ".join(units))
    return result


# ---- CLI --------------------------------------------------------------------------


def _node_name() -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", socket.gethostname().split(".")[0]) or "node"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--model", help="ANTHROPIC_MODEL id to switch to")
    p.add_argument("--effort", help="CLAUDE_CODE_EFFORT_LEVEL (e.g. high)")
    p.add_argument("--label", help="CCC_MODEL_LABEL shown by the bridges")
    p.add_argument("--slug", help="file-name slug, e.g. fable51-high")
    p.add_argument("--dry-run", action="store_true", help="show the plan; write nothing")
    p.add_argument("--restart", action="store_true", help="restart active units (idle-gated)")
    p.add_argument("--rollback", type=Path, metavar="BACKUP_DIR", help="undo a switch")
    p.add_argument("--backup-root", type=Path, default=Path("~/.ccc-node/backups"))
    p.add_argument("--node", default=_node_name(), help="node label for the backup dir")
    p.add_argument("--systemd-dir", type=Path, default=Path("/etc/systemd/system"))
    p.add_argument("--env-dir", type=Path, default=Path("/root/.config/ccc-main"))
    p.add_argument("--claude-dir", type=Path, default=Path("~/.claude"))
    p.add_argument(
        "--bridge-data-dirs",
        action="append",
        default=[],
        metavar="DIR[,DIR]",
        help=f"dirs holding sessions.json (default: {','.join(DEFAULT_DATA_DIRS)})",
    )
    p.add_argument("--idle-timeout", type=float, default=1800.0, help="seconds per unit")
    p.add_argument("--idle-interval", type=float, default=15.0)
    p.add_argument("--verify-timeout", type=float, default=60.0)
    p.add_argument("--proc", type=Path, default=Path("/proc"), help=argparse.SUPPRESS)
    p.add_argument(
        "--self-cgroup", type=Path, default=Path("/proc/self/cgroup"), help=argparse.SUPPRESS
    )
    return p


def _validate(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    missing = [n for n in ("model", "effort", "label", "slug") if not getattr(args, n)]
    if missing:
        parser.error("required: " + ", ".join(f"--{n}" for n in missing))
    checks = (
        ("--model", _MODEL.fullmatch(args.model)),
        ("--effort", _EFFORT.fullmatch(args.effort)),
        ("--slug", _SLUG.fullmatch(args.slug)),
        ("--label", not _LABEL_FORBIDDEN.search(args.label) and len(args.label) <= 80),
    )
    for flag, ok in checks:
        if not ok:
            parser.error(f"invalid {flag}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    for name in ("backup_root", "claude_dir", "systemd_dir", "env_dir"):
        setattr(args, name, Path(os.path.normpath(os.path.expanduser(getattr(args, name)))))
    if args.rollback is not None:
        args.rollback = Path(os.path.expanduser(args.rollback)).resolve()
        return do_rollback(args)
    _validate(args, parser)
    dirs = [d for chunk in args.bridge_data_dirs for d in chunk.split(",") if d]
    args.bridge_data_dirs = dirs or list(DEFAULT_DATA_DIRS)
    return switch(args)


if __name__ == "__main__":
    sys.exit(main())
