#!/usr/bin/env python3
"""env-file-guard: find systemd units that reference an EnvironmentFile (#2571).

Motivating outage (2026-10-27, #2571): on one node a node-local model-switch apply.sh
replaced the Matrix/Telegram drop-ins and moved
``/root/.config/ccc-main/matrix-opus55-high.env`` into a ``retired/`` backup
dir. ``ccc-hande-bridge.service`` -- whose drop-ins
``scripts/install-hande-bridge.sh`` copies from the Matrix unit -- still had a
*required* ``EnvironmentFile=`` on that path, so its next restart failed with
``Failed to load environment files`` and the bridge stayed down for 14.7 h.

Model-switch scripts are node-local, so the guard lives here as a shared tool
they (and operators / doctor) call:

  env-file-guard.py users <path>...   units still referencing <path>
                                      exit 1 if any do (do NOT mv/rm it), else 0
  env-file-guard.py check             units whose REQUIRED EnvironmentFiles
                                      are missing; exit 1 if any

Scope: the system manager, plus each ``--user <name>`` manager
(``systemctl --user -M <name>@``). Units are those *loaded* units that
``systemctl list-units --all --type=service`` returns for ``--unit-glob``
(default ``ccc-*`` and ``native-relay-*``), or every loaded service with
``--all``. Limit: a unit file that exists on disk but is not loaded (never
started, not enabled, not a dependency) is not listed by list-units and is
therefore not inspected; run ``systemctl daemon-reload`` first after editing
drop-ins so the manager's view matches the files.

Only paths are ever printed -- never the contents of an env file.
``SYSTEMCTL`` overrides the systemctl binary (tests use a fake one).
Exit codes: 0 clean, 1 references / missing files found, 2 usage or
systemctl error.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass

DEFAULT_GLOBS = ("ccc-*", "native-relay-*")
TIMEOUT_S = 15.0

# One `systemctl show -p EnvironmentFiles --value` entry: "<path> (ignore_errors=yes|no)".
# Entries are separated by newlines or spaces depending on the systemd version.
_ENTRY = re.compile(r"\s*(.+?)\s+\(ignore_errors=(yes|no)\)")


class SystemctlError(RuntimeError):
    pass


@dataclass(frozen=True)
class EnvRef:
    manager: str  # "system" or "user:<name>"
    unit: str
    path: str
    optional: bool
    exists: bool


def parse_environment_files(value: str) -> list[tuple[str, bool]]:
    """Parse the EnvironmentFiles property into (path, optional) pairs."""
    return [(m.group(1), m.group(2) == "yes") for m in _ENTRY.finditer(value or "")]


def _systemctl_bin() -> str:
    return os.environ.get("SYSTEMCTL") or "systemctl"


def _uid_of(name: str) -> int | None:
    try:
        import pwd

        return pwd.getpwnam(name).pw_uid
    except (ImportError, KeyError):
        return None


def _manager_invocation(manager: str) -> tuple[list[str], dict[str, str] | None]:
    """systemctl args (and env override) that address ``manager``.

    ``--user -M <name>@`` needs systemd-machined, which many VPS images lack
    ("Failed to connect to bus: No medium found"). When the requested user is
    the caller itself, talk to its own user manager with plain ``--user``
    instead, supplying XDG_RUNTIME_DIR when a non-login shell (ssh command,
    systemd service) left it unset.
    """
    if manager == "system":
        return [], None
    name = manager.split(":", 1)[1]
    uid = _uid_of(name)
    if uid is not None and uid == os.geteuid():
        env = None
        if not os.environ.get("XDG_RUNTIME_DIR"):
            env = {**os.environ, "XDG_RUNTIME_DIR": f"/run/user/{uid}"}
        return ["--user"], env
    return ["--user", "-M", f"{name}@"], None


def _run(manager: str, args: list[str]) -> str:
    margs, env = _manager_invocation(manager)
    cmd = [_systemctl_bin(), *margs, *args]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=TIMEOUT_S, check=False, env=env
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SystemctlError(f"{' '.join(cmd)}: {exc}") from exc
    if proc.returncode != 0:
        err = (proc.stderr or "").strip().splitlines()
        raise SystemctlError(
            f"{' '.join(cmd)}: exit {proc.returncode}: {err[0] if err else ''}"
        )
    return proc.stdout


def list_units(manager: str, globs: list[str]) -> list[str]:
    out = _run(
        manager,
        ["list-units", "--all", "--type=service", "--plain", "--no-legend", *globs],
    )
    units = []
    for line in out.splitlines():
        # The unit name is the first token ending in .service; older systemd
        # versions may prefix a status bullet even with --plain.
        name = next((t for t in line.split() if t.endswith(".service")), None)
        if name and name not in units:
            units.append(name)
    return units


def collect_refs(
    managers: list[str], globs: list[str]
) -> tuple[list[EnvRef], list[str]]:
    """Return every EnvironmentFiles entry plus the "<manager> <unit>" list inspected."""
    refs, inspected = [], []
    for manager in managers:
        for unit in list_units(manager, globs):
            inspected.append(f"{manager} {unit}")
            value = _run(manager, ["show", "-p", "EnvironmentFiles", "--value", unit])
            for path, optional in parse_environment_files(value):
                refs.append(
                    EnvRef(manager, unit, path, optional, os.path.exists(path))
                )
    return refs, inspected


def _keys(path: str) -> set[str]:
    absolute = os.path.abspath(os.path.expanduser(path))
    return {os.path.normpath(absolute), os.path.realpath(absolute)}


def find_users(refs: list[EnvRef], targets: list[str]) -> list[EnvRef]:
    wanted = set().union(*(_keys(t) for t in targets)) if targets else set()
    return [r for r in refs if _keys(r.path) & wanted]


def find_missing(refs: list[EnvRef]) -> list[EnvRef]:
    return [r for r in refs if not r.optional and not r.exists]


def _kind(ref: EnvRef) -> str:
    return "optional" if ref.optional else "required"


def _print_text(command: str, hits: list[EnvRef], targets: list[str]) -> None:
    for r in hits:
        state = "present" if r.exists else "MISSING"
        print(f"{r.manager}\t{r.unit}\t{_kind(r)}\t{state}\t{r.path}")
    if command == "users":
        if hits:
            print(
                f"env-file-guard: {len(hits)} reference(s) still point at "
                f"{', '.join(targets)} -- update those units before moving/removing it",
                file=sys.stderr,
            )
        else:
            print(f"env-file-guard: no inspected unit references {', '.join(targets)}")
    elif hits:
        print(
            f"env-file-guard: {len(hits)} required EnvironmentFile(s) missing -- "
            "those units will fail to start",
            file=sys.stderr,
        )
    else:
        print("env-file-guard: every required EnvironmentFile exists")


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="machine-readable output")
    common.add_argument(
        "--user",
        action="append",
        default=[],
        metavar="NAME",
        help="also inspect NAME's user manager (systemctl --user -M NAME@); repeatable",
    )
    common.add_argument(
        "--unit-glob",
        action="append",
        default=[],
        metavar="GLOB",
        help=f"unit glob to inspect; repeatable (default: {' '.join(DEFAULT_GLOBS)})",
    )
    common.add_argument("--all", action="store_true", help="inspect every loaded service")

    parser = argparse.ArgumentParser(
        description="Find systemd units that reference (or miss) EnvironmentFiles."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    users = sub.add_parser(
        "users", parents=[common], help="units referencing the given env file path(s)"
    )
    users.add_argument("paths", nargs="+", metavar="PATH")
    sub.add_parser(
        "check", parents=[common], help="units whose required EnvironmentFiles are missing"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    managers = ["system", *(f"user:{u}" for u in args.user)]
    globs = [] if args.all else (args.unit_glob or list(DEFAULT_GLOBS))
    targets = getattr(args, "paths", [])
    try:
        refs, inspected = collect_refs(managers, globs)
    except SystemctlError as exc:
        print(f"env-file-guard: systemctl failed: {exc}", file=sys.stderr)
        return 2
    hits = find_users(refs, targets) if args.command == "users" else find_missing(refs)
    if args.json:
        payload = {
            "command": args.command,
            "managers": managers,
            "unit_globs": globs or ["*"],
            "targets": targets,
            "units_inspected": inspected,
            "hits": [{**asdict(r), "kind": _kind(r)} for r in hits],
            "ok": not hits,
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"env-file-guard: inspected {len(inspected)} unit(s)", file=sys.stderr)
        _print_text(args.command, hits, targets)
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
