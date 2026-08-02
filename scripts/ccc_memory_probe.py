#!/usr/bin/env python3
"""Body-free, read-only nunchi + MemPalace readiness probe."""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
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
        # Readiness runs on every memory check, including large Chroma stores.
        # A full PRAGMA quick_check can scan hundreds of MB and stall the bridge;
        # a bounded read-only connection plus the required schema query proves
        # that the store is readable without turning status into an audit.
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.2)
        try:
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA busy_timeout=200")
            count = int(db.execute(count_sql).fetchone()[0])
        finally:
            db.close()
        return "ok", count
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return "error", 0


def crontab_text() -> str:
    if "CCC_NUNCHI_CRONTAB_TEXT" in os.environ:
        return os.environ["CCC_NUNCHI_CRONTAB_TEXT"]
    try:
        result = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True, timeout=0.5, check=False
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


def parse_repair_status(text: str) -> dict[str, object]:
    result: dict[str, object] = {
        "status": "unknown",
        "sqlite_count": -1,
        "hnsw_count": -1,
        "divergence": -1,
    }
    if "Palace is initialized but empty" in text:
        result["status"] = "empty"
        return result
    in_drawers = False
    for raw in text.splitlines():
        line = raw.strip()
        if line == "[drawers]":
            in_drawers = True
            continue
        if line.startswith("[") and line.endswith("]"):
            in_drawers = False
        if not in_drawers:
            continue
        match = re.fullmatch(r"sqlite count:\s*([0-9,]+)", line)
        if match:
            result["sqlite_count"] = int(match.group(1).replace(",", ""))
            continue
        match = re.fullmatch(r"hnsw count:\s*([0-9,]+)", line)
        if match:
            result["hnsw_count"] = int(match.group(1).replace(",", ""))
            continue
        match = re.fullmatch(r"divergence:\s*([0-9,]+)", line)
        if match:
            result["divergence"] = int(match.group(1).replace(",", ""))
            continue
        match = re.fullmatch(r"status:\s*([A-Z]+)", line)
        if match:
            result["status"] = match.group(1).lower()
    return result


def mempalace_index_probe(mp_cli: Path, home: Path) -> dict[str, object]:
    override = os.environ.get("CCC_NUNCHI_MEMPALACE_REPAIR_STATUS_TEXT")
    if override is not None:
        return parse_repair_status(override)
    try:
        result = subprocess.run(
            [str(mp_cli), "repair-status"],
            cwd=home,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "sqlite_count": -1, "hnsw_count": -1, "divergence": -1}
    except OSError:
        return {"status": "error", "sqlite_count": -1, "hnsw_count": -1, "divergence": -1}
    if result.returncode != 0:
        return {"status": "error", "sqlite_count": -1, "hnsw_count": -1, "divergence": -1}
    return parse_repair_status(result.stdout + "\n" + result.stderr)


def mempalace_refresh_probe(path: Path, now: int) -> dict[str, object]:
    payload: dict[str, object] = {
        "status": "missing",
        "provider": "unknown",
        "exit_code": -1,
        "age_seconds": -1,
    }
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return payload
    except (OSError, json.JSONDecodeError):
        payload["status"] = "invalid"
        return payload
    if not isinstance(doc, dict) or doc.get("schema") != "ccc.nunchi.mempalace-refresh.v1":
        payload["status"] = "invalid"
        return payload
    state = doc.get("state")
    provider = doc.get("provider")
    exit_code = doc.get("exit_code")
    started = doc.get("started_at")
    finished = doc.get("finished_at")
    if (
        state not in {"running", "ok", "error"}
        or provider not in {"claude", "codex"}
        or not isinstance(exit_code, int)
        or not isinstance(started, int)
        or not isinstance(finished, int)
    ):
        payload["status"] = "invalid"
        return payload
    reference = finished if finished > 0 else started
    return {
        "status": state,
        "provider": provider,
        "exit_code": exit_code,
        "age_seconds": max(0, now - reference) if reference > 0 else -1,
    }


def probe_nunchi(
    state: Path, claude: Path, nunchi_home: Path, ttl: int, now: int
) -> tuple[dict[str, object], str, int, str]:
    mode_file = state / "nunchi.mode"
    try:
        mode = mode_file.read_text(encoding="utf-8").strip()
    except OSError:
        mode = "off"

    db_path = nunchi_home / "facts.db"
    db_integrity, facts = (
        sqlite_probe(db_path, "SELECT COUNT(*) FROM peer_facts")
        if mode == "on"
        else ("skipped", 0)
    )
    snapshot = nunchi_home / "snapshot.md"
    snapshot_bytes = snapshot.stat().st_size if snapshot.is_file() else 0
    snapshot_primary = False
    if mode == "on":
        try:
            with snapshot.open("rb") as handle:
                snapshot_head = handle.read(512)
            snapshot_primary = "nunchi working memory" in snapshot_head.decode(
                "utf-8", errors="replace"
            )
        except OSError:
            pass

    cron = crontab_text() if mode == "on" else ""
    marker_lines = [line for line in cron.splitlines() if "nunchi:#816" in line]
    codex_feeds = sum("codex-feed.sh" in line for line in marker_lines)
    claude_feeds = sum("ingest-cron.sh" in line for line in marker_lines)
    feed_count = codex_feeds + claude_feeds
    feed_kind = "codex" if codex_feeds else ("claude" if claude_feeds else "missing")
    managed_refresh_count = sum("mempalace-refresh.sh" in line for line in marker_lines)
    legacy_sweep_count = sum(
        "mempalace-refresh.sh" not in line and "mempalace" in line and " sweep " in line
        for line in marker_lines
    )
    sweep_count = managed_refresh_count + legacy_sweep_count
    bench_count = sum("bench.sh" in line for line in marker_lines)
    standalone = standalone_hook_count(claude / "settings.local.json") if mode == "on" else 0
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
        if feed_kind == "codex" and standalone:
            nunchi_reasons.append("standalone-sessionstart")
        if feed_kind == "claude" and standalone != 1:
            nunchi_reasons.append("sessionstart-count")
        nunchi_status = "ok" if not nunchi_reasons else "degraded"
    else:
        nunchi_status = "off"

    payload: dict[str, object] = {
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
            "managed_refresh_count": managed_refresh_count,
            "legacy_sweep_count": legacy_sweep_count,
            "bench_count": bench_count,
        },
    }
    return payload, mode, sweep_count, feed_kind


def probe_mempalace(
    home: Path,
    nunchi_home: Path,
    mode: str,
    provider: str,
    sweep_count: int,
    ttl: int,
    now: int,
) -> dict[str, object]:
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
    mp_integrity, embeddings = (
        sqlite_probe(palace, "SELECT COUNT(*) FROM embeddings")
        if mode == "on"
        else ("skipped", 0)
    )
    index = (
        mempalace_index_probe(mp_cli, home)
        if mode == "on" and mp_cli.is_file() and palace.is_file()
        else {"status": "skipped", "sqlite_count": -1, "hnsw_count": -1, "divergence": -1}
    )
    refresh = mempalace_refresh_probe(nunchi_home / "mempalace-refresh.status.json", now)
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
        if index["status"] == "diverged":
            mp_reasons.append("index-diverged")
        elif index["status"] in {"error", "timeout"}:
            mp_reasons.append("index-" + str(index["status"]))
        if required and mp_cli.is_file() and sweep_count != 1:
            mp_reasons.append("sweep-count")
        refresh_status = str(refresh["status"])
        refresh_age = int(refresh["age_seconds"])
        if refresh_status in {"error", "invalid"}:
            mp_reasons.append("refresh-" + refresh_status)
        elif refresh_status in {"running", "ok"} and refresh_age > ttl:
            mp_reasons.append("refresh-stale")
        elif refresh_status == "missing" and age_seconds(palace, now) > ttl:
            mp_reasons.append("refresh-missing")
        if refresh_status != "missing" and refresh["provider"] != provider:
            mp_reasons.append("refresh-provider")
        mp_status = "ok" if not mp_reasons else "degraded"

    return {
        "status": mp_status,
        "required": required,
        "reasons": mp_reasons,
        "cli_installed": mp_cli.is_file(),
        "palace_exists": palace.is_file(),
        "integrity": mp_integrity,
        "embeddings": embeddings,
        "age_seconds": age_seconds(palace, now),
        "index": index,
        "refresh": refresh,
    }


def main() -> int:
    home = Path(os.environ.get("HOME") or "/root")
    state = Path(os.environ.get("CCC_STATE_DIR") or home / ".claude/state")
    claude = Path(os.environ.get("CCC_CLAUDE_DIR") or home / ".claude")
    nunchi_home = Path(os.environ.get("NUNCHI_HOME") or home / ".nunchi")
    ttl = int(os.environ.get("CCC_MEMORY_CACHE_TTL_SEC") or "21600")
    now_raw = os.environ.get("CCC_MEMORY_CHECK_NOW_EPOCH") or ""
    now = int(now_raw) if now_raw.isdigit() else int(time.time())
    nunchi, mode, sweep_count, provider = probe_nunchi(state, claude, nunchi_home, ttl, now)
    payload = {
        "nunchi": nunchi,
        "mempalace": probe_mempalace(
            home, nunchi_home, mode, provider, sweep_count, ttl, now
        ),
    }
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
