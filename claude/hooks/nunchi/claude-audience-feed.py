#!/usr/bin/env python3
"""Audience-scoped nunchi mirror for Claude-provider nodes (#1921).

``ingest-cron.sh`` hands off here when its cron line carries
``CCC_NUNCHI_AUDIENCE_SCOPED=1``. Inputs are the same two zero-LLM-cost sources
the global Claude lane mirrors — ``distill-history`` snapshots and the bridge
distill journal — but every item is routed to exactly one audience store:

    <audience-root>/<scope>/nunchi/{facts.db,snapshot.md}

The route comes ONLY from the bridge's per-turn sidecar
(``<audience-root>/<scope>/claude/session-map/<session_id>.json``, contract in
``bridge/core/claude_audience_sidecar.py``), keyed by the Claude session id
that the item names. Claude transcripts all share ``~/.claude/projects`` and
carry no audience of their own, so there is nothing else to trust:

* no sidecar for the session            -> ``unmapped``  (skipped)
* valid sidecars under two scopes       -> ``ambiguous`` (skipped)
* malformed/unsafe/mislabelled sidecar,
  or a journal job whose own route is
  missing or disagrees with the sidecar -> ``invalid``   (skipped)

Skipped items are counted, never guessed and never marked seen: a session that
gains a sidecar later is routed on a later tick. Output is one body-free line
of counters for the caller's status tick; no session ids, scopes, paths or
fact text are printed.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

SIDECAR_SCHEMA = "ccc.claude.session-audience.v1"
SIDECAR_MAX_BYTES = 4096
SESSION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
PRIVATE_SCOPE_RE = re.compile(r"private-[0-9a-f]{32}")
MAX_SCOPES = 64

HERE = Path(__file__).resolve().parent


def _load_adapter():
    spec = importlib.util.spec_from_file_location("nunchi_bridge_journal", HERE / "bridge-journal.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("bridge-journal adapter unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _owner_only_dir(path: Path) -> bool:
    try:
        meta = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISDIR(meta.st_mode)
        and meta.st_uid == os.geteuid()
        and not stat.S_IMODE(meta.st_mode) & 0o077
    )


def scope_dirs(root: Path, limit: int = MAX_SCOPES) -> list[Path]:
    """Canonical, owner-only direct children — the piri-feed walk, verbatim."""
    if not root.is_absolute() or not _owner_only_dir(root):
        return []
    out: list[Path] = []
    for child in sorted(root.iterdir(), key=lambda item: item.name):
        if len(out) >= limit:
            break
        if child.name != "shared" and not PRIVATE_SCOPE_RE.fullmatch(child.name):
            continue
        if _owner_only_dir(child):
            out.append(child)
    return out


def _read_sidecar(path: Path) -> dict | None:
    """Owner-only regular single-link file, bounded, never through a symlink."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        meta = os.fstat(fd)
        if (
            not stat.S_ISREG(meta.st_mode)
            or meta.st_uid != os.geteuid()
            or stat.S_IMODE(meta.st_mode) & 0o077
            or meta.st_nlink != 1
            or meta.st_size > SIDECAR_MAX_BYTES
        ):
            return None
        raw = os.read(fd, SIDECAR_MAX_BYTES + 1)
    finally:
        os.close(fd)
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


class SidecarIndex:
    """session_id -> {(kind, scope)} built once per tick; poisoned ids fail closed."""

    def __init__(self, root: Path) -> None:
        self.routes: dict[str, set[tuple[str, str]]] = {}
        self.poisoned: set[str] = set()
        self.records = 0
        self.scopes = scope_dirs(root)
        for scope_dir in self.scopes:
            self._scan(scope_dir)

    def _scan(self, scope_dir: Path) -> None:
        scope = scope_dir.name
        kind = "shared" if scope == "shared" else "private"
        claude_dir = scope_dir / "claude"
        map_dir = claude_dir / "session-map"
        if not map_dir.exists() and not map_dir.is_symlink():
            return
        if not (_owner_only_dir(claude_dir) and _owner_only_dir(map_dir)):
            # An unsafe map directory cannot vouch for any of its entries.
            # Its sessions are unknown, so poison nothing we cannot name and
            # simply contribute no routes (they stay unmapped).
            return
        try:
            entries = sorted(os.listdir(map_dir))
        except OSError:
            return
        for entry in entries:
            if entry.startswith(".") or not entry.endswith(".json"):
                continue  # atomic-write temp files and foreign names
            sid = entry[: -len(".json")]
            if not SESSION_ID_RE.fullmatch(sid):
                continue
            record = _read_sidecar(map_dir / entry)
            if (
                record is None
                or record.get("schema") != SIDECAR_SCHEMA
                or record.get("provider") != "claude"
                or record.get("session_id") != sid
                or record.get("memory_audience") != kind
                or record.get("memory_scope") != scope
            ):
                self.poisoned.add(sid)
                continue
            self.records += 1
            self.routes.setdefault(sid, set()).add((kind, scope))

    def resolve(self, sid: str) -> tuple[str, tuple[str, str] | None]:
        if not sid or not SESSION_ID_RE.fullmatch(sid):
            return "unmapped", None
        if sid in self.poisoned:
            return "invalid", None
        routes = self.routes.get(sid) or set()
        if not routes:
            return "unmapped", None
        if len(routes) > 1:
            return "ambiguous", None
        return "routed", next(iter(routes))


class Feed:
    def __init__(self, args: argparse.Namespace) -> None:
        self.root = Path(args.audience_root)
        self.nunchi_py = args.nunchi_py
        self.seen_path = Path(args.seen)
        self.projects_root = args.projects_root
        self.index = SidecarIndex(self.root)
        self.counts = dict.fromkeys(
            ("sources", "ingested", "retired", "deferred", "unmapped", "ambiguous", "invalid"), 0
        )
        try:
            self.seen = set(self.seen_path.read_text(encoding="utf-8").splitlines())
        except OSError:
            self.seen = set()
        self.touched: set[str] = set()

    def _mark_seen(self, path: Path) -> None:
        with self.seen_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{path}\n")
        self.seen.add(str(path))

    def _scope_env(self, scope: str) -> dict[str, str] | None:
        scope_dir = self.root / scope
        home = scope_dir / "nunchi"
        if not _owner_only_dir(scope_dir):
            return None
        if not home.exists() and not home.is_symlink():
            try:
                home.mkdir(mode=0o700)
            except OSError:
                return None
        if not _owner_only_dir(home):
            return None
        env = os.environ.copy()
        env.update(
            {
                "NUNCHI_HOME": str(home),
                "NUNCHI_DB": str(home / "facts.db"),
                "NUNCHI_SNAPSHOT": str(home / "snapshot.md"),
            }
        )
        return env

    def _route(self, sid: str, declared: tuple[object, object] | None = None):
        outcome, route = self.index.resolve(sid)
        if outcome == "routed" and declared is not None and tuple(declared) != route:
            # Defence in depth: a bridge journal job carries the route its own
            # local sink uses. It must agree with the sidecar, or neither is
            # trusted — this also refuses a routeless (pre-scoping) job whose
            # session later gained a sidecar under some other surface.
            outcome, route = "invalid", None
        if outcome != "routed":
            self.counts[outcome] += 1
        return route

    def _ingest(self, payload: dict, route: tuple[str, str]) -> bool:
        kind, scope = route
        env = self._scope_env(scope)
        if env is None:
            self.counts["invalid"] += 1
            return False
        payload = dict(payload)
        payload.update({"provider": "claude", "memory_audience": kind, "memory_scope": scope})
        result = subprocess.run(
            [sys.executable, self.nunchi_py, "ingest", "-"],
            input=json.dumps(payload, ensure_ascii=False),
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        if result.returncode != 0:
            self.counts["deferred"] += 1
            return False
        self.touched.add(scope)
        self.counts["ingested"] += 1
        return True

    def history(self, directory: Path) -> None:
        if not directory.is_dir():
            return
        self.counts["sources"] += 1
        for path in sorted(directory.glob("*.json")):
            if not path.is_file() or str(path) in self.seen:
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            except (OSError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            route = self._route(str(payload.get("session_id") or ""))
            if route is not None and self._ingest(payload, route):
                self._mark_seen(path)

    def journal(self, directory: Path, adapter) -> None:
        if not directory.is_dir():
            return
        self.counts["sources"] += 1
        for path in sorted(directory.glob("*.json")):
            if not path.is_file() or str(path) in self.seen:
                continue
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self.counts["deferred"] += 1
                continue
            payload = adapter.adapt(job, self.projects_root)
            if payload is None:
                status = job.get("status") if isinstance(job, dict) else None
                if status in adapter._TERMINAL:
                    self._mark_seen(path)
                    self.counts["retired"] += 1
                else:
                    self.counts["deferred"] += 1
                continue
            declared = (job.get("memory_audience"), job.get("memory_scope"))
            route = self._route(str(job.get("thread_id") or ""), declared)
            if route is not None and self._ingest(payload, route):
                self._mark_seen(path)

    def snapshots(self) -> None:
        for scope in sorted(self.touched):
            env = self._scope_env(scope)
            if env is None:
                continue
            subprocess.run(
                [sys.executable, self.nunchi_py, "snapshot", "--limit", "25"],
                capture_output=True,
                env=env,
                check=False,
            )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--audience-root", required=True)
    parser.add_argument("--history", required=True)
    parser.add_argument("--journal", required=True)
    parser.add_argument("--seen", required=True)
    parser.add_argument("--nunchi-py", required=True)
    parser.add_argument("--projects-root", default=None)
    args = parser.parse_args(argv)

    feed = Feed(args)
    feed.history(Path(args.history))
    feed.journal(Path(args.journal), _load_adapter())
    feed.snapshots()
    c = feed.counts
    # Fixed field order; the shell caller `read`s it positionally.
    print(
        c["sources"], c["ingested"], c["retired"], c["deferred"],
        c["unmapped"], c["ambiguous"], c["invalid"],
        len(feed.index.scopes), feed.index.records,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
