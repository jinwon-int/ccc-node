#!/usr/bin/env python3
"""Body-free, read-only nunchi + MemPalace readiness probe."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sqlite3
import stat
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


def _termux_palace_error() -> dict[str, object]:
    return {
        "backend": "sqlite_exact",
        "palace_exists": False,
        "integrity": "error",
        "embeddings": 0,
        "age_seconds": -1,
        "index": {
            "status": "error",
            "sqlite_count": -1,
            "hnsw_count": -1,
            "divergence": -1,
        },
    }


def _safe_owned_file(path: Path, mode: int | None = None) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISREG(info.st_mode)
        and info.st_uid == os.getuid()
        and info.st_nlink == 1
        and (mode is None or stat.S_IMODE(info.st_mode) == mode)
    )


def _safe_owned_directory(path: Path, mode: int) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISDIR(info.st_mode)
        and info.st_uid == os.getuid()
        and stat.S_IMODE(info.st_mode) == mode
    )


def _termux_metadata_state(metadata: Path) -> tuple[str, str | None]:
    if not metadata.exists() and not metadata.is_symlink():
        return "absent", None
    if not _safe_owned_directory(metadata.parent, 0o700) or not _safe_owned_file(
        metadata, 0o600
    ):
        return "invalid", None
    try:
        if metadata.stat().st_size > 4096:
            return "invalid", None
        doc = json.loads(metadata.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "invalid", None
    if not isinstance(doc, dict) or doc.get("schema") != "ccc.termux-mempalace.install.v1":
        return "invalid", None
    if doc.get("enabled") is False:
        return "disabled", None
    container = doc.get("container")
    if (
        doc.get("enabled") is not True
        or doc.get("version") != "3.6.0"
        or doc.get("state") != "ready"
        or doc.get("provider") not in {"codex", "claude", "piri"}
        or type(doc.get("updated_at")) is not int
        or int(doc["updated_at"]) <= 0
        or not isinstance(container, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", container) is None
    ):
        return "invalid", None
    return "enabled", container


def _termux_container_base(prefix: Path, container: str) -> Path | None:
    roots = (
        prefix / "var/lib/proot-distro/containers" / container / "rootfs",
        prefix / "var/lib/proot-distro/installed-rootfs" / container,
    )
    root = next(
        (
            candidate
            for candidate in roots
            if candidate.is_dir() and not candidate.is_symlink()
        ),
        None,
    )
    return None if root is None else root / "opt/ccc-mempalace"


def _safe_termux_container_base(base: Path) -> bool:
    marker = base / ".ccc-node-managed"
    if not _safe_owned_directory(base, 0o700) or not _safe_owned_file(marker, 0o600):
        return False
    try:
        return (
            marker.stat().st_size <= 64
            and marker.read_text(encoding="utf-8").strip()
            == "ccc-node #867 managed container"
        )
    except (OSError, UnicodeError):
        return False


def termux_sqlite_exact_probe(
    nunchi_home: Path, now: int
) -> dict[str, object] | None:
    """Read the managed PRoot palace without exposing transcript bodies.

    None means that the optional Termux topology is not enabled. Once its
    owner-only metadata says enabled, malformed metadata, an unsafe marker,
    or an unreadable DB fails readiness closed instead of falling back to the
    unrelated native Chroma path.
    """

    state, container = _termux_metadata_state(
        nunchi_home / "termux-mempalace/status.json"
    )
    if state in {"absent", "disabled"}:
        return None
    error = _termux_palace_error()
    if state != "enabled" or container is None:
        return error
    base = _termux_container_base(Path(os.environ.get("PREFIX") or ""), container)
    if base is None or not _safe_termux_container_base(base):
        return error

    palace = base / "palace/sqlite_exact.sqlite3"
    if not _safe_owned_file(palace):
        return {**error, "integrity": "missing" if not palace.exists() else "error"}
    integrity, drawers = sqlite_probe(palace, "SELECT COUNT(*) FROM documents")
    index_status = "ok" if integrity == "ok" else "error"
    index_count = drawers if integrity == "ok" else -1
    return {
        "backend": "sqlite_exact",
        "palace_exists": True,
        "integrity": integrity,
        "embeddings": drawers,
        "age_seconds": age_seconds(palace, now),
        # sqlite_exact has no separate ANN/HNSW copy. Project its single
        # authoritative drawer count into the existing index health shape.
        "index": {
            "status": index_status,
            "sqlite_count": index_count,
            "hnsw_count": index_count,
            "divergence": 0 if integrity == "ok" else -1,
        },
    }


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
        or provider not in {"claude", "codex", "piri"}
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


# Installer-rendered managed entries end with the `# nunchi:#816` marker plus,
# since #1081/#1140, a content-stamped ` gen=h_<sha256:12>` suffix
# (scripts/lib/installer-gen-stamp.sh). The probe only recognizes and strips
# the trailer here; validating the stamp against the checkout is the
# doctor/self-update gen-drift lane's job, so the suffix is accepted, not
# compared. (#1174: the end-anchored pre-stamp form read every stamped entry
# as unmanaged and falsely reported feed/refresh/bench-count degraded.)
_MANAGED_MARKER = re.compile(r"\s+# nunchi:#816(?:\s+gen=h_[0-9a-f]{12})?\s*$")


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


_MANAGED_ENV_KEYS = (
    "CCC_STATE_DIR",
    "NUNCHI_HOME",
    "NUNCHI_DB",
    "NUNCHI_SNAPSHOT",
    "CCC_NUNCHI_MEMPALACE_STATUS",
    "CCC_NUNCHI_MEMPALACE_CLI",
    "CCC_NUNCHI_AUDIENCE_SCOPED",
    "CCC_NUNCHI_AUDIENCE_ROOT",
)


_AUDIENCE_COUNT_FIELDS = (
    "scope_count",
    "private_count",
    "shared_count",
    "session_roots",
    "nunchi_db_partitions",
    "snapshot_partitions",
    "mempalace_index_partitions",
    "mempalace_status_partitions",
    "refresh_ok",
    "refresh_running",
    "refresh_degraded",
    "refresh_error",
    "refresh_invalid",
    "refresh_stale",
    "invalid_entries",
)


def _safe_audience_item(path: Path, *, directory: bool) -> bool:
    try:
        item = path.lstat()
    except OSError:
        return False
    expected = stat.S_ISDIR(item.st_mode) if directory else stat.S_ISREG(item.st_mode)
    return bool(
        expected
        and item.st_uid == os.geteuid()
        and not stat.S_IMODE(item.st_mode) & 0o077
        and (directory or item.st_nlink == 1)
    )


def _audience_refresh_state(path: Path, *, ttl: int, now: int) -> tuple[str, bool]:
    try:
        if path.stat().st_size > 8192:
            raise ValueError
        record = json.loads(path.read_text(encoding="utf-8"))
        state = record.get("state")
        timestamp = record.get("started_at") if state == "running" else record.get("finished_at")
        if (
            record.get("schema") != "ccc.nunchi.mempalace-refresh.v1"
            or record.get("provider") != "piri"
            or state not in {"ok", "running", "degraded", "error"}
            or not isinstance(timestamp, int)
            or isinstance(timestamp, bool)
            or timestamp < 0
        ):
            raise ValueError
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return "invalid", False
    return str(state), state in {"ok", "running"} and now - timestamp > ttl


def _audience_child_counts(
    child: Path, *, ttl: int, now: int
) -> dict[str, int] | None:
    private = re.fullmatch(r"private-[0-9a-f]{32}", child.name) is not None
    if child.name != "shared" and not private:
        return None
    if not _safe_audience_item(child, directory=True):
        return None
    counts = {field: 0 for field in _AUDIENCE_COUNT_FIELDS}
    counts["scope_count"] = 1
    counts["private_count" if private else "shared_count"] = 1
    session_root = child / "piri/sessions"
    if session_root.exists() or session_root.is_symlink():
        field = "session_roots" if _safe_audience_item(session_root, directory=True) else "invalid_entries"
        counts[field] += 1
    for path, field in (
        (child / "nunchi/facts.db", "nunchi_db_partitions"),
        (child / "nunchi/snapshot.md", "snapshot_partitions"),
        (child / "nunchi/mempalace-refresh.status.json", "mempalace_status_partitions"),
    ):
        if not path.exists() and not path.is_symlink():
            continue
        if not _safe_audience_item(path, directory=False):
            counts["invalid_entries"] += 1
            continue
        counts[field] += 1
        if field == "mempalace_status_partitions":
            state, stale = _audience_refresh_state(path, ttl=ttl, now=now)
            counts[f"refresh_{state}"] += 1
            counts["refresh_stale"] += int(stale)
    palace = child / "mempalace-home/.mempalace/palace"
    indexes = [palace / name for name in ("chroma.sqlite3", "memory.db")]
    existing = [path for path in indexes if path.exists() or path.is_symlink()]
    if existing:
        field = (
            "mempalace_index_partitions"
            if any(_safe_audience_item(path, directory=False) for path in existing)
            else "invalid_entries"
        )
        counts[field] += 1
    return counts


def probe_audience_scopes(
    root: Path, enabled: bool, *, ttl: int, now: int
) -> dict[str, object]:
    """Return body-free counts for canonical owner-only audience partitions."""

    result: dict[str, object] = {
        "enabled": enabled,
        "root_status": "disabled" if not enabled else "missing",
        **{field: 0 for field in _AUDIENCE_COUNT_FIELDS},
    }
    if not enabled:
        return result
    if not root.is_absolute() or not _safe_audience_item(root, directory=True):
        result["root_status"] = "unsafe" if root.exists() else "missing"
        return result
    result["root_status"] = "ok"
    try:
        children = list(root.iterdir())[:257]
    except OSError:
        result["root_status"] = "unreadable"
        return result
    if len(children) > 256:
        result["invalid_entries"] = 1
        children = children[:256]
    for child in children:
        counts = _audience_child_counts(child, ttl=ttl, now=now)
        if counts is None:
            result["invalid_entries"] = int(result["invalid_entries"]) + 1
            continue
        for field, value in counts.items():
            result[field] = int(result[field]) + value
    return result


def managed_cron_environment(cron: str) -> tuple[dict[str, str], list[str]]:
    """Recover path configuration shared by recognized managed commands."""

    commands = cron_commands(cron, managed_only=True)
    environments = [
        command_environment(tokens)
        for tokens in commands
        if any(
            is_bash_script(command_invocation(tokens), script)
            for script in (
                "codex-feed.sh",
                "piri-feed.sh",
                "ingest-cron.sh",
                "mempalace-refresh.sh",
                "bench.sh",
            )
        )
    ]
    recovered: dict[str, str] = {}
    conflicts: list[str] = []
    for key in _MANAGED_ENV_KEYS:
        values = {environment[key] for environment in environments if environment.get(key)}
        if len(values) == 1:
            recovered[key] = values.pop()
        elif len(values) > 1:
            conflicts.append(key)
    return recovered, conflicts


def inspect_managed_cron(
    cron: str,
) -> tuple[str, int, list[tuple[str, str, dict[str, str]]], int, int]:
    commands = cron_commands(cron, managed_only=True)
    invocations = [command_invocation(command) for command in commands]
    codex_feeds = sum(is_bash_script(command, "codex-feed.sh") for command in invocations)
    claude_feeds = sum(is_bash_script(command, "ingest-cron.sh") for command in invocations)
    piri_feeds = sum(is_bash_script(command, "piri-feed.sh") for command in invocations)
    feed_count = codex_feeds + claude_feeds + piri_feeds
    feed_kind = (
        "codex"
        if codex_feeds == 1 and claude_feeds == 0 and piri_feeds == 0
        else "claude"
        if claude_feeds == 1 and codex_feeds == 0 and piri_feeds == 0
        else "piri"
        if piri_feeds == 1 and codex_feeds == 0 and claude_feeds == 0
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


def nunchi_ingest_probe(path: Path, now: int) -> dict[str, object]:
    """Read the mirror ingester's per-tick receipt (`ccc.nunchi.ingest.v1`).

    #1018 stayed invisible for weeks because the ingester's only observable
    output was the fact count, and a node carrying facts from a previous
    provider looked healthy while mirroring nothing. The receipt records what a
    tick actually saw, so "ran but had no input" stops reading as success.
    """
    payload: dict[str, object] = {
        "status": "missing",
        "sources": -1,
        "ingested": -1,
        "age_seconds": -1,
    }
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return payload
    except (OSError, json.JSONDecodeError):
        return {**payload, "status": "invalid"}
    if not isinstance(doc, dict) or doc.get("schema") != "ccc.nunchi.ingest.v1":
        return {**payload, "status": "invalid"}
    finished = doc.get("finished_at")
    sources = doc.get("sources")
    ingested = doc.get("ingested")
    # bool is a subclass of int in Python; readiness accepts exact integers only.
    if (
        type(finished) is not int
        or type(sources) is not int
        or type(ingested) is not int
        or finished <= 0
        or not 0 <= sources <= 2
        or ingested < 0
    ):
        return {**payload, "status": "invalid"}
    # Cron and the probe may straddle a clock correction. Permit five minutes,
    # but reject farther-future records instead of making them look fresh.
    if finished > now + 300:
        return {**payload, "status": "invalid"}
    return {
        "status": "ok",
        "sources": sources,
        "ingested": ingested,
        "age_seconds": max(0, now - finished),
    }


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
    refresh_contract_required: bool,
    configuration_conflicts: list[str],
    ingest: dict[str, object],
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
    if refresh_contract_required:
        if len(refreshes) != 1:
            reasons.append("refresh-count")
        if legacy_sweep_count != 0:
            reasons.append("legacy-sweep")
        if len(refreshes) == 1 and refreshes[0][0] != feed_kind:
            reasons.append("refresh-provider-arg")
        if len(refreshes) == 1 and not refreshes[0][1]:
            reasons.append("refresh-target-arg")
    if configuration_conflicts:
        reasons.append("cron-env-conflict")
    if bench_count != 1:
        reasons.append("bench-count")
    if feed_kind in ("codex", "piri") and standalone:
        reasons.append("standalone-sessionstart")
    if feed_kind == "claude" and standalone != 1:
        reasons.append("sessionstart-count")
    reasons.extend(
        nunchi_ingest_reasons(
            ingest=ingest, feed_kind=feed_kind, facts_age=facts_age, ttl=ttl
        )
    )
    return reasons


def nunchi_ingest_reasons(
    *,
    ingest: dict[str, object],
    feed_kind: str,
    facts_age: int,
    ttl: int,
) -> list[str]:
    """Judge the mirror ingester's receipt.

    Only the claude feed writes one; codex and piri feeds have their own
    producers and must not be judged against a file they never create.
    """
    if feed_kind != "claude":
        return []
    status = ingest.get("status")
    if status == "ok":
        if ingest.get("sources") == 0:
            # The exact #1018 shape: the ingester runs, finds no producer at
            # all, and every other signal still reads healthy.
            return ["ingest-sourceless"]
        if int(ingest.get("age_seconds") or 0) > ttl:
            return ["ingest-stale"]
        return []
    if status == "invalid":
        return ["ingest-invalid"]
    # A receipt-less node is only suspect once its facts also stopped
    # advancing; a node that simply predates the receipt keeps working.
    return ["ingest-unobserved"] if facts_age > ttl else []


def probe_nunchi(
    state: Path,
    claude: Path,
    nunchi_home: Path,
    db_path: Path,
    snapshot: Path,
    cron: str,
    ttl: int,
    now: int,
    *,
    mempalace_required: bool,
    mempalace_cli_installed: bool,
    configuration_conflicts: list[str],
) -> tuple[dict[str, object], str, str, int, int, dict[str, str]]:
    mode_file = state / "nunchi.mode"
    try:
        mode = mode_file.read_text(encoding="utf-8").strip()
    except OSError:
        mode = "off"

    db_integrity, facts = (
        sqlite_probe(db_path, "SELECT COUNT(*) FROM peer_facts")
        if mode == "on"
        else ("skipped", 0)
    )
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

    cron = cron if mode == "on" else ""
    feed_kind, feed_count, refreshes, legacy_sweep_count, bench_count = (
        inspect_managed_cron(cron)
    )
    managed_refresh_count = len(refreshes)
    refresh_contract_required = (
        mempalace_required
        or mempalace_cli_installed
        or managed_refresh_count != 0
        or legacy_sweep_count != 0
    )
    standalone = standalone_hook_count(claude / "settings.local.json") if mode == "on" else 0
    hook_installed = (claude / "hooks/nunchi/nunchi.py").is_file()

    ingest_status_path = Path(
        os.environ.get("CCC_NUNCHI_INGEST_STATUS")
        or (nunchi_home / "ingest.status.json")
    )
    ingest = nunchi_ingest_probe(ingest_status_path, now)

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
            refresh_contract_required=refresh_contract_required,
            configuration_conflicts=configuration_conflicts,
            ingest=ingest,
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
        "ingest": ingest,
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


def is_mempalace_required(home: Path) -> bool:
    prefix = os.environ.get("PREFIX") or ""
    default_required = "/com.termux/" not in prefix and not str(home).startswith(
        "/data/data/"
    )
    required_raw = (os.environ.get("CCC_NUNCHI_MEMPALACE_REQUIRED") or "").lower()
    return default_required if not required_raw else required_raw not in {
        "0",
        "false",
        "off",
        "no",
    }


def resolve_mempalace_cli(home: Path, environment: dict[str, str]) -> Path:
    configured_cli = os.environ.get("CCC_NUNCHI_MEMPALACE_CLI") or environment.get(
        "CCC_NUNCHI_MEMPALACE_CLI"
    )
    mp_cli = Path(configured_cli) if configured_cli else home / ".local/bin/mempalace"
    if not configured_cli and not mp_cli.is_file():
        found = shutil.which("mempalace")
        mp_cli = Path(found) if found else mp_cli
    return mp_cli


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
    required = is_mempalace_required(home)
    mp_cli = resolve_mempalace_cli(home, refresh_environment)
    palace = home / ".mempalace/palace/chroma.sqlite3"
    termux_palace = termux_sqlite_exact_probe(nunchi_home, now) if mode == "on" else None
    if termux_palace is not None:
        backend = str(termux_palace["backend"])
        palace_exists = bool(termux_palace["palace_exists"])
        mp_integrity = str(termux_palace["integrity"])
        embeddings = int(termux_palace["embeddings"])
        palace_age = int(termux_palace["age_seconds"])
        index = dict(termux_palace["index"])
    else:
        backend = "chroma"
        palace_exists = palace.is_file()
        mp_integrity, embeddings = (
            sqlite_probe(palace, "SELECT COUNT(*) FROM embeddings")
            if mode == "on"
            else ("skipped", 0)
        )
        index = (
            mempalace_index_probe(mp_cli, home)
            if mode == "on" and mp_cli.is_file() and palace_exists
            else {
                "status": "skipped",
                "sqlite_count": -1,
                "hnsw_count": -1,
                "divergence": -1,
            }
        )
        palace_age = age_seconds(palace, now)
    status_path = Path(
        os.environ.get("CCC_NUNCHI_MEMPALACE_STATUS")
        or refresh_environment.get("CCC_NUNCHI_MEMPALACE_STATUS")
        or nunchi_home / "mempalace-refresh.status.json"
    )
    refresh = mempalace_refresh_probe(status_path, now)
    mp_reasons: list[str] = []
    if mode != "on":
        mp_status = "off"
    elif (
        not required
        and not mp_cli.is_file()
        and managed_refresh_count == 0
        and legacy_sweep_count == 0
    ):
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
        refresh_age_raw = refresh["age_seconds"]
        refresh_age = (
            refresh_age_raw
            if isinstance(refresh_age_raw, int) and not isinstance(refresh_age_raw, bool)
            else -1
        )
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
        "backend": backend,
        "palace_exists": palace_exists,
        "integrity": mp_integrity,
        "embeddings": embeddings,
        "age_seconds": palace_age,
        "index": index,
        "refresh": refresh,
    }


def main() -> int:
    home = Path(os.environ.get("HOME") or "/root")
    cron = crontab_text()
    managed_environment, configuration_conflicts = managed_cron_environment(cron)
    scoped_raw = (
        os.environ.get("CCC_NUNCHI_AUDIENCE_SCOPED")
        or managed_environment.get("CCC_NUNCHI_AUDIENCE_SCOPED")
        or "0"
    )
    audience_scoped = scoped_raw.strip().lower() not in {"", "0", "false", "off", "no"}
    audience_root = Path(
        os.environ.get("CCC_NUNCHI_AUDIENCE_ROOT")
        or managed_environment.get("CCC_NUNCHI_AUDIENCE_ROOT")
        or "/nonexistent"
    )
    state = Path(
        os.environ.get("CCC_STATE_DIR")
        or managed_environment.get("CCC_STATE_DIR")
        or home / ".claude/state"
    )
    claude = Path(os.environ.get("CCC_CLAUDE_DIR") or home / ".claude")
    nunchi_home = Path(
        os.environ.get("NUNCHI_HOME")
        or managed_environment.get("NUNCHI_HOME")
        or home / ".nunchi"
    )
    db_path = Path(
        os.environ.get("NUNCHI_DB")
        or managed_environment.get("NUNCHI_DB")
        or nunchi_home / "facts.db"
    )
    snapshot = Path(
        os.environ.get("NUNCHI_SNAPSHOT")
        or managed_environment.get("NUNCHI_SNAPSHOT")
        or nunchi_home / "snapshot.md"
    )
    ttl = int(os.environ.get("CCC_MEMORY_CACHE_TTL_SEC") or "21600")
    now_raw = os.environ.get("CCC_MEMORY_CHECK_NOW_EPOCH") or ""
    now = int(now_raw) if now_raw.isdigit() else int(time.time())
    audience_probe = probe_audience_scopes(
        audience_root, audience_scoped, ttl=ttl, now=now
    )
    required = is_mempalace_required(home)
    mp_cli = resolve_mempalace_cli(home, managed_environment)
    (
        nunchi,
        mode,
        provider,
        managed_refresh_count,
        legacy_sweep_count,
        refresh_environment,
    ) = probe_nunchi(
        state,
        claude,
        nunchi_home,
        db_path,
        snapshot,
        cron,
        ttl,
        now,
        mempalace_required=required,
        mempalace_cli_installed=mp_cli.is_file(),
        configuration_conflicts=configuration_conflicts,
    )
    nunchi["audience_scoped"] = audience_probe
    mempalace_environment = {**managed_environment, **refresh_environment}
    mempalace = probe_mempalace(
        home,
        nunchi_home,
        mode,
        provider,
        managed_refresh_count,
        legacy_sweep_count,
        mempalace_environment,
        ttl,
        now,
    )
    if audience_scoped and mode == "on":
        scoped_reasons: list[str] = []
        if audience_probe["root_status"] != "ok":
            scoped_reasons.append("audience-root-" + str(audience_probe["root_status"]))
        if int(audience_probe["invalid_entries"]):
            scoped_reasons.append("audience-invalid")
        expected = int(audience_probe["session_roots"])
        status_count = int(audience_probe["mempalace_status_partitions"])
        if bool(mempalace["cli_installed"]) or bool(mempalace["required"]):
            if expected != status_count:
                scoped_reasons.append("refresh-count")
            if int(audience_probe["refresh_error"]):
                scoped_reasons.append("refresh-error")
            if int(audience_probe["refresh_invalid"]):
                scoped_reasons.append("refresh-invalid")
            if int(audience_probe["refresh_stale"]):
                scoped_reasons.append("refresh-stale")
            if int(audience_probe["refresh_degraded"]):
                scoped_reasons.append("refresh-degraded")
            scoped_status = "ok" if not scoped_reasons else "degraded"
        else:
            scoped_status = "optional"
        mempalace.update(
            {
                "status": scoped_status,
                "reasons": scoped_reasons,
                "backend": "audience-partitioned",
                "palace_exists": int(
                    audience_probe["mempalace_index_partitions"]
                )
                > 0,
                "audience_scoped": audience_probe,
            }
        )
    payload = {
        "nunchi": nunchi,
        "mempalace": mempalace,
    }
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
