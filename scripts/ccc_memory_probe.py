#!/usr/bin/env python3
"""Body-free, read-only nunchi + MemPalace readiness probe."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path


def age_seconds(path: Path, now: int) -> int:
    try:
        return max(0, now - int(path.stat().st_mtime))
    except OSError:
        return -1


def sqlite_probe(path: Path, count_sql: str) -> tuple[str, int]:
    if not path.is_file():
        return "missing", 0
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            integrity = str(db.execute("PRAGMA quick_check").fetchone()[0])
            count = int(db.execute(count_sql).fetchone()[0])
        finally:
            db.close()
        return integrity, count
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return "error", 0


def crontab_text() -> str:
    if "CCC_NUNCHI_CRONTAB_TEXT" in os.environ:
        return os.environ["CCC_NUNCHI_CRONTAB_TEXT"]
    try:
        result = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True, timeout=3, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout if result.returncode in (0, 1) else ""


def standalone_hook_count(settings: Path) -> int:
    try:
        doc = json.loads(settings.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    return sum(
        "nunchi/sessionstart.sh" in str(hook.get("command") or "")
        for group in doc.get("hooks", {}).get("SessionStart", [])
        for hook in group.get("hooks", [])
        if isinstance(hook, dict)
    )


def main() -> int:
    home = Path(os.environ.get("HOME") or "/root")
    state = Path(os.environ.get("CCC_STATE_DIR") or home / ".claude/state")
    claude = Path(os.environ.get("CCC_CLAUDE_DIR") or home / ".claude")
    nunchi_home = Path(os.environ.get("NUNCHI_HOME") or home / ".nunchi")
    ttl = int(os.environ.get("CCC_MEMORY_CACHE_TTL_SEC") or "21600")
    now_raw = os.environ.get("CCC_MEMORY_CHECK_NOW_EPOCH") or ""
    now = int(now_raw) if now_raw.isdigit() else int(time.time())

    mode_file = state / "nunchi.mode"
    try:
        mode = mode_file.read_text(encoding="utf-8").strip()
    except OSError:
        mode = "off"

    db_path = nunchi_home / "facts.db"
    db_integrity, facts = sqlite_probe(db_path, "SELECT COUNT(*) FROM peer_facts")
    snapshot = nunchi_home / "snapshot.md"
    snapshot_bytes = snapshot.stat().st_size if snapshot.is_file() else 0
    try:
        snapshot_primary = "nunchi working memory" in snapshot.read_text(
            encoding="utf-8", errors="replace"
        )[:512]
    except OSError:
        snapshot_primary = False

    cron = crontab_text()
    marker_lines = [line for line in cron.splitlines() if "nunchi:#816" in line]
    codex_feeds = sum("codex-feed.sh" in line for line in marker_lines)
    claude_feeds = sum("ingest-cron.sh" in line for line in marker_lines)
    feed_count = codex_feeds + claude_feeds
    feed_kind = "codex" if codex_feeds else ("claude" if claude_feeds else "missing")
    sweep_count = sum("mempalace" in line and " sweep " in line for line in marker_lines)
    bench_count = sum("bench.sh" in line for line in marker_lines)
    standalone = standalone_hook_count(claude / "settings.local.json")
    hook_installed = (claude / "hooks/nunchi/nunchi.py").is_file()

    nunchi_reasons: list[str] = []
    if mode == "on":
        if not hook_installed:
            nunchi_reasons.append("hook-missing")
        if db_integrity != "ok":
            nunchi_reasons.append("db-" + db_integrity)
        if not snapshot_primary or snapshot_bytes == 0:
            nunchi_reasons.append("snapshot-missing")
        if facts == 0 and age_seconds(db_path, now) > ttl:
            nunchi_reasons.append("facts-empty-stale")
        if feed_count != 1:
            nunchi_reasons.append("feed-count")
        if bench_count != 1:
            nunchi_reasons.append("bench-count")
        if standalone:
            nunchi_reasons.append("standalone-sessionstart")
        nunchi_status = "ok" if not nunchi_reasons else "degraded"
    else:
        nunchi_status = "off"

    prefix = os.environ.get("PREFIX") or ""
    default_required = "/com.termux/" not in prefix and not str(home).startswith("/data/data/")
    required_raw = (os.environ.get("CCC_NUNCHI_MEMPALACE_REQUIRED") or "").lower()
    required = default_required if not required_raw else required_raw not in {
        "0", "false", "off", "no"
    }
    mp_cli = home / ".local/bin/mempalace"
    if not mp_cli.is_file():
        found = shutil.which("mempalace")
        mp_cli = Path(found) if found else mp_cli
    palace = home / ".mempalace/palace/chroma.sqlite3"
    mp_integrity, embeddings = sqlite_probe(palace, "SELECT COUNT(*) FROM embeddings")
    mp_reasons: list[str] = []
    if mode != "on":
        mp_status = "off"
    elif not required and not mp_cli.is_file():
        mp_status = "optional"
    else:
        if not mp_cli.is_file():
            mp_reasons.append("cli-missing")
        if mp_integrity != "ok":
            mp_reasons.append("palace-" + mp_integrity)
        elif embeddings == 0:
            mp_reasons.append("embeddings-empty")
        if required and mp_cli.is_file() and sweep_count != 1:
            mp_reasons.append("sweep-count")
        mp_status = "ok" if not mp_reasons else "degraded"

    payload = {
        "nunchi": {
            "status": nunchi_status,
            "mode": mode,
            "reasons": nunchi_reasons,
            "hook_installed": hook_installed,
            "standalone_sessionstart_hooks": standalone,
            "db": {
                "exists": db_path.is_file(),
                "integrity": db_integrity,
                "facts": facts,
                "bytes": db_path.stat().st_size if db_path.is_file() else 0,
                "age_seconds": age_seconds(db_path, now),
            },
            "snapshot": {
                "exists": snapshot.is_file(),
                "primary_header": snapshot_primary,
                "bytes": snapshot_bytes,
                "age_seconds": age_seconds(snapshot, now),
            },
            "cron": {
                "feed": feed_kind,
                "feed_count": feed_count,
                "sweep_count": sweep_count,
                "bench_count": bench_count,
            },
        },
        "mempalace": {
            "status": mp_status,
            "required": required,
            "reasons": mp_reasons,
            "cli_installed": mp_cli.is_file(),
            "palace_exists": palace.is_file(),
            "integrity": mp_integrity,
            "embeddings": embeddings,
            "age_seconds": age_seconds(palace, now),
        },
    }
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
