#!/usr/bin/env python3
"""Audience-scoped nunchi mirror for Claude-provider nodes (#1921).

``ingest-cron.sh`` hands off here when its cron line carries
``CCC_NUNCHI_AUDIENCE_SCOPED=1``. Inputs are the zero-LLM-cost sources the
global Claude lane mirrors, each routed to exactly one audience store:

    <audience-root>/<scope>/nunchi/{facts.db,snapshot.md}

* the bridge distill journal (the bridge-managed lane), and
* each scope's OWN ``<root>/<scope>/state/distill-history`` — bridge sessions
  run their hooks with ``CCC_STATE_DIR`` pointed there — ingested only into
  that scope and only when the sidecar maps the session to that same scope.

The node-wide ``~/.claude/state/distill-history`` is deliberately NOT read in
this mode: bridge-managed distill never writes it, so everything there comes
from non-bridge sessions (the operator's terminal CLI, cron, workers). Routing
those by session id alone would let ``claude --resume <room session>`` on the
owner's terminal push private CLI facts into the shared store.

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
import time
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


def _trusted_dir(path: Path) -> bool:
    """Owner directory, not a symlink, not writable by group/other."""
    try:
        meta = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISDIR(meta.st_mode)
        and meta.st_uid == os.geteuid()
        and not stat.S_IMODE(meta.st_mode) & 0o022
    )


def _trusted_file(path: Path) -> bool:
    try:
        meta = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISREG(meta.st_mode)
        and meta.st_uid == os.geteuid()
        and meta.st_nlink == 1
        and not stat.S_IMODE(meta.st_mode) & 0o022
    )


def _max_age_days() -> int:
    """CCC_NUNCHI_CLAUDE_SIDECAR_MAX_AGE_DAYS (default 90, 0 disables, max 3650)."""
    raw = os.environ.get("CCC_NUNCHI_CLAUDE_SIDECAR_MAX_AGE_DAYS", "90")
    try:
        value = int(raw)
    except ValueError:
        return 90
    return value if 0 <= value <= 3650 else 90


class SidecarIndex:
    """session_id -> {(kind, scope)} built once per tick; poisoned ids fail closed."""

    def __init__(self, root: Path) -> None:
        self.routes: dict[str, set[tuple[str, str]]] = {}
        self.poisoned: set[str] = set()
        self.files: dict[str, list[Path]] = {}
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
            self.files.setdefault(sid, []).append(map_dir / entry)
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
        # Session ids that still have an unseen input item; their sidecars
        # are never pruned, whatever their age.
        self.pending: set[str] = set()
        self.pruned = 0

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

    def _route(
        self,
        sid: str,
        declared: tuple[object, object] | None = None,
        pinned_scope: str | None = None,
    ):
        outcome, route = self.index.resolve(sid)
        if outcome == "routed" and pinned_scope is not None and route[1] != pinned_scope:
            # A scope's own distill-history item whose session the sidecar
            # maps to a different audience: neither location is trusted.
            outcome, route = "invalid", None
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

    def scoped_history(self) -> None:
        """Each scope's own distill-history, into that scope only (never node-wide)."""
        for scope_dir in self.index.scopes:
            state = scope_dir / "state"
            directory = state / "distill-history"
            if not (_trusted_dir(state) and _trusted_dir(directory)):
                continue
            self.counts["sources"] += 1
            for path in sorted(directory.glob("*.json")):
                if str(path) in self.seen or not _trusted_file(path):
                    continue
                try:
                    payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
                except (OSError, ValueError):
                    continue
                if not isinstance(payload, dict):
                    continue
                sid = str(payload.get("session_id") or "")
                route = self._route(sid, pinned_scope=scope_dir.name)
                if route is not None and self._ingest(payload, route):
                    self._mark_seen(path)
                else:
                    self.pending.add(sid)

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
                    if isinstance(job, dict):
                        self.pending.add(str(job.get("thread_id") or ""))
                continue
            sid = str(job.get("thread_id") or "")
            declared = (job.get("memory_audience"), job.get("memory_scope"))
            route = self._route(sid, declared)
            if route is not None and self._ingest(payload, route):
                self._mark_seen(path)
            else:
                self.pending.add(sid)

    def prune(self, max_age_days: int, now: float) -> None:
        """Remove sidecars untouched for ``max_age_days`` with no pending input.

        The bridge rewrites a session's sidecar on every turn, so mtime is the
        last turn. Only owner-owned regular files inside an owner-only map dir
        are removed (a symlink entry is never followed or unlinked); a session
        that is resumed later simply gets a fresh sidecar on its next turn.
        """
        if max_age_days <= 0:
            return
        cutoff = now - max_age_days * 86400
        for sid, paths in self.index.files.items():
            if sid in self.pending:
                continue
            for path in paths:
                try:
                    meta = path.lstat()
                    if (
                        stat.S_ISREG(meta.st_mode)
                        and meta.st_uid == os.geteuid()
                        and meta.st_mtime < cutoff
                    ):
                        path.unlink()
                        self.pruned += 1
                except OSError:
                    continue

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
    parser.add_argument("--journal", required=True)
    parser.add_argument("--seen", required=True)
    parser.add_argument("--nunchi-py", required=True)
    parser.add_argument("--projects-root", default=None)
    args = parser.parse_args(argv)

    feed = Feed(args)
    feed.scoped_history()
    feed.journal(Path(args.journal), _load_adapter())
    feed.snapshots()
    feed.prune(_max_age_days(), time.time())
    c = feed.counts
    # Fixed field order; the shell caller `read`s it positionally.
    print(
        c["sources"], c["ingested"], c["retired"], c["deferred"],
        c["unmapped"], c["ambiguous"], c["invalid"],
        len(feed.index.scopes), feed.index.records, feed.pruned,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
