#!/usr/bin/env python3
"""Body-free, read-only nunchi + MemPalace readiness probe."""

from __future__ import annotations

import json
import os
import re
import shlex
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


_REPAIR_PATTERNS = {
    "sqlite_count": re.compile(r"sqlite count:\s*([0-9]+(?:,[0-9]{3})*)"),
    "hnsw_count": re.compile(r"hnsw count:\s*([0-9]+(?:,[0-9]{3})*)"),
    "divergence": re.compile(r"divergence:\s*([0-9]+(?:,[0-9]{3})*)"),
    "reported_status": re.compile(r"status:\s*([A-Z]+)"),
}
_REPAIR_LABELS = ("sqlite count:", "hnsw count:", "divergence:", "status:")


def _record_repair_value(line: str, values: dict[str, int | str]) -> bool:
    """Record one drawer scalar; return False for duplicate/malformed labels."""

    for key, pattern in _REPAIR_PATTERNS.items():
        match = pattern.fullmatch(line)
        if not match:
            continue
        if key in values:
            return False
        raw_value = match.group(1)
        values[key] = (
            raw_value if key == "reported_status" else int(raw_value.replace(",", ""))
        )
        return True
    return not line.startswith(_REPAIR_LABELS)


def parse_repair_status(text: str) -> dict[str, object]:
    """Project MemPalace 3.6 drawer counts, failing closed on ambiguity."""
    unknown: dict[str, object] = {
        "status": "unknown",
        "sqlite_count": -1,
        "hnsw_count": -1,
        "divergence": -1,
    }
    if "Palace is initialized but empty" in text:
        return {**unknown, "status": "empty"}

    in_drawers = False
    saw_drawers = False
    malformed = False
    values: dict[str, int | str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if line == "[drawers]":
            if saw_drawers:
                malformed = True
            saw_drawers = True
            in_drawers = True
            continue
        if line.startswith("[") and line.endswith("]"):
            in_drawers = False
        if not in_drawers:
            continue
        if not _record_repair_value(line, values):
            malformed = True

    counts = {
        key: values.get(key, -1)
        for key in ("sqlite_count", "hnsw_count", "divergence")
    }
    result = {**unknown, **counts}
    if malformed:
        return {**result, "status": "malformed"}
    if not saw_drawers:
        return result
    if set(values) != {"sqlite_count", "hnsw_count", "divergence", "reported_status"}:
        return {**result, "status": "partial"}

    sqlite_count = int(values["sqlite_count"])
    hnsw_count = int(values["hnsw_count"])
    divergence = int(values["divergence"])
    reported = str(values["reported_status"])
    consistent = divergence == abs(sqlite_count - hnsw_count)
    if reported == "OK" and consistent and divergence == 0 and sqlite_count == hnsw_count:
        return {**result, "status": "ok"}
    if reported == "DIVERGED" and consistent and divergence > 0:
        return {**result, "status": "diverged"}
    return {**result, "status": "malformed"}


def mempalace_index_probe(mp_cli: Path, home: Path) -> dict[str, object]:
    override = os.environ.get("CCC_NUNCHI_MEMPALACE_REPAIR_STATUS_TEXT")
    if override is not None:
        return parse_repair_status(override)
    unavailable = {
        "status": "error",
        "sqlite_count": -1,
        "hnsw_count": -1,
        "divergence": -1,
    }
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
        return {**unavailable, "status": "timeout"}
    except OSError:
        return unavailable
    if result.returncode != 0:
        return unavailable
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
        return {**payload, "status": "invalid"}
    if not isinstance(doc, dict) or doc.get("schema") != "ccc.nunchi.mempalace-refresh.v1":
        return {**payload, "status": "invalid"}

    state = doc.get("state")
    provider = doc.get("provider")
    exit_code = doc.get("exit_code")
    started = doc.get("started_at")
    finished = doc.get("finished_at")
    # bool is a subclass of int in Python; readiness accepts exact integers only.
    if (
        state not in {"running", "ok", "error"}
        or provider not in {"claude", "codex"}
        or type(exit_code) is not int
        or type(started) is not int
        or type(finished) is not int
    ):
        return {**payload, "status": "invalid"}
    valid_state = (
        (state == "running" and started > 0 and finished == 0 and exit_code == -1)
        or (state == "ok" and 0 < started <= finished and exit_code == 0)
        or (
            state == "error"
            and 0 < started <= finished
            and 1 <= exit_code <= 255
        )
    )
    if not valid_state:
        return {**payload, "status": "invalid"}
    reference = started if state == "running" else finished
    # Cron and the probe may straddle a clock correction. Permit five minutes,
    # but reject farther-future records instead of making them look fresh.
    if reference > now + 300 or started > now + 300:
        return {**payload, "status": "invalid"}
    return {
        "status": state,
        "provider": provider,
        "exit_code": exit_code,
        "age_seconds": max(0, now - reference),
    }


_MANAGED_MARKER = re.compile(r"\s+# nunchi:#816\s*$")


def cron_commands(cron: str, *, managed_only: bool) -> list[list[str]]:
    commands: list[list[str]] = []
    for line in cron.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        marker = _MANAGED_MARKER.search(line)
        if managed_only and not marker:
            continue
        command_line = line[: marker.start()] if marker else line
        fields = command_line.split(maxsplit=5)
        if len(fields) != 6:
            continue
        try:
            commands.append(shlex.split(fields[5], comments=False, posix=True))
        except ValueError:
            continue
    return commands


def token_basename(token: str) -> str:
    return token.rstrip("/").rsplit("/", 1)[-1]


def command_invocation(tokens: list[str]) -> list[str]:
    index = 0
    while index < len(tokens) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[index]):
        index += 1
    return tokens[index:]


def command_environment(tokens: list[str]) -> dict[str, str]:
    environment: dict[str, str] = {}
    for token in tokens:
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token):
            break
        key, value = token.split("=", 1)
        # crond removes this escape before invoking the shell.
        environment[key] = value.replace(r"\%", "%")
    return environment


def is_bash_script(command: list[str], script: str) -> bool:
    return (
        len(command) >= 2
        and token_basename(command[0]) == "bash"
        and token_basename(command[1]) == script
    )


def inspect_managed_cron(
    cron: str,
) -> tuple[str, int, list[tuple[str, str, dict[str, str]]], int, int]:
    commands = cron_commands(cron, managed_only=True)
    invocations = [command_invocation(command) for command in commands]
    codex_feeds = sum(is_bash_script(command, "codex-feed.sh") for command in invocations)
    claude_feeds = sum(is_bash_script(command, "ingest-cron.sh") for command in invocations)
    feed_count = codex_feeds + claude_feeds
    feed_kind = (
        "codex"
        if codex_feeds == 1 and claude_feeds == 0
        else "claude"
        if claude_feeds == 1 and codex_feeds == 0
        else "missing"
        if feed_count == 0
        else "mixed"
    )
    legacy_sweep_count = sum(
        1
        for command in commands
        for index, token in enumerate(command)
        if token_basename(token) == "mempalace"
        and index + 1 < len(command)
        and command[index + 1] == "sweep"
    )
    refreshes = [
        (
            invocation[2] if len(invocation) >= 3 else "",
            invocation[3] if len(invocation) >= 4 else "",
            command_environment(tokens),
        )
        for tokens, invocation in zip(commands, invocations, strict=True)
        if is_bash_script(invocation, "mempalace-refresh.sh")
    ]
    bench_count = sum(is_bash_script(command, "bench.sh") for command in invocations)
    return feed_kind, feed_count, refreshes, legacy_sweep_count, bench_count


def nunchi_readiness_reasons(
    *,
    hook_installed: bool,
    db_integrity: str,
    facts: int,
    facts_age: int,
    snapshot_primary: bool,
    snapshot_bytes: int,
    feed_kind: str,
    feed_count: int,
    refreshes: list[tuple[str, str, dict[str, str]]],
    legacy_sweep_count: int,
    bench_count: int,
    standalone: int,
    ttl: int,
) -> list[str]:
    reasons: list[str] = []
    if not hook_installed:
        reasons.append("hook-missing")
    if db_integrity != "ok":
        reasons.append("db-" + db_integrity)
    if not snapshot_primary or snapshot_bytes == 0:
        reasons.append("snapshot-missing")
    if facts == 0 and facts_age > ttl:
        reasons.append("facts-empty-stale")
    if feed_count != 1:
        reasons.append("feed-count")
    if len(refreshes) != 1:
        reasons.append("refresh-count")
    if legacy_sweep_count != 0:
        reasons.append("legacy-sweep")
    if len(refreshes) == 1 and refreshes[0][0] != feed_kind:
        reasons.append("refresh-provider-arg")
    if len(refreshes) == 1 and not refreshes[0][1]:
        reasons.append("refresh-target-arg")
    if bench_count != 1:
        reasons.append("bench-count")
    if feed_kind == "codex" and standalone:
        reasons.append("standalone-sessionstart")
    if feed_kind == "claude" and standalone != 1:
        reasons.append("sessionstart-count")
    return reasons


def probe_nunchi(
    state: Path, claude: Path, nunchi_home: Path, ttl: int, now: int
) -> tuple[dict[str, object], str, str, int, int, dict[str, str]]:
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
    feed_kind, feed_count, refreshes, legacy_sweep_count, bench_count = (
        inspect_managed_cron(cron)
    )
    managed_refresh_count = len(refreshes)
    standalone = standalone_hook_count(claude / "settings.local.json") if mode == "on" else 0
    hook_installed = (claude / "hooks/nunchi/nunchi.py").is_file()

    if mode == "on":
        nunchi_reasons = nunchi_readiness_reasons(
            hook_installed=hook_installed,
            db_integrity=db_integrity,
            facts=facts,
            facts_age=age_seconds(db_path, now),
            snapshot_primary=snapshot_primary,
            snapshot_bytes=snapshot_bytes,
            feed_kind=feed_kind,
            feed_count=feed_count,
            refreshes=refreshes,
            legacy_sweep_count=legacy_sweep_count,
            bench_count=bench_count,
            standalone=standalone,
            ttl=ttl,
        )
        nunchi_status = "ok" if not nunchi_reasons else "degraded"
    else:
        nunchi_reasons = []
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
            "sweep_count": managed_refresh_count + legacy_sweep_count,
            "managed_refresh_count": managed_refresh_count,
            "legacy_sweep_count": legacy_sweep_count,
            "bench_count": bench_count,
        },
    }
    refresh_environment = refreshes[0][2] if managed_refresh_count == 1 else {}
    return (
        payload,
        mode,
        feed_kind,
        managed_refresh_count,
        legacy_sweep_count,
        refresh_environment,
    )


def probe_mempalace(
    home: Path,
    nunchi_home: Path,
    mode: str,
    provider: str,
    managed_refresh_count: int,
    legacy_sweep_count: int,
    refresh_environment: dict[str, str],
    ttl: int,
    now: int,
) -> dict[str, object]:
    prefix = os.environ.get("PREFIX") or ""
    default_required = "/com.termux/" not in prefix and not str(home).startswith("/data/data/")
    required_raw = (os.environ.get("CCC_NUNCHI_MEMPALACE_REQUIRED") or "").lower()
    required = default_required if not required_raw else required_raw not in {
        "0", "false", "off", "no"
    }
    configured_cli = (
        os.environ.get("CCC_NUNCHI_MEMPALACE_CLI")
        or refresh_environment.get("CCC_NUNCHI_MEMPALACE_CLI")
    )
    mp_cli = Path(configured_cli) if configured_cli else home / ".local/bin/mempalace"
    if not configured_cli and not mp_cli.is_file():
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
        else {
            "status": "skipped",
            "sqlite_count": -1,
            "hnsw_count": -1,
            "divergence": -1,
        }
    )
    status_path = Path(
        os.environ.get("CCC_NUNCHI_MEMPALACE_STATUS")
        or refresh_environment.get("CCC_NUNCHI_MEMPALACE_STATUS")
        or nunchi_home / "mempalace-refresh.status.json"
    )
    refresh = mempalace_refresh_probe(status_path, now)
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
        index_status = str(index["status"])
        if index_status != "ok":
            mp_reasons.append("index-" + index_status)
        if required and mp_cli.is_file():
            if managed_refresh_count != 1:
                mp_reasons.append("refresh-count")
            if legacy_sweep_count != 0:
                mp_reasons.append("legacy-sweep")
        refresh_status = str(refresh["status"])
        refresh_age = int(refresh["age_seconds"])
        if refresh_status in {"error", "invalid", "missing"}:
            mp_reasons.append("refresh-" + refresh_status)
        elif refresh_status in {"running", "ok"} and refresh_age > ttl:
            mp_reasons.append("refresh-stale")
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
    (
        nunchi,
        mode,
        provider,
        managed_refresh_count,
        legacy_sweep_count,
        refresh_environment,
    ) = probe_nunchi(state, claude, nunchi_home, ttl, now)
    payload = {
        "nunchi": nunchi,
        "mempalace": probe_mempalace(
            home,
            nunchi_home,
            mode,
            provider,
            managed_refresh_count,
            legacy_sweep_count,
            refresh_environment,
            ttl,
            now,
        ),
    }
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
