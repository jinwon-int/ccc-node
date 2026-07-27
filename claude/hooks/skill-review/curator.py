#!/usr/bin/env python3
"""Usage telemetry and deterministic stale/archive lifecycle for autosave-managed skills (#752).

Scope and safety contract:

- Only ``autosave-managed`` skills (ownership provenance from ownership.py) are
  lifecycle-eligible. user-owned, managed/bundled, external/repo-installed and
  unknown skills are observed for telemetry but never auto-transitioned.
- No permanent deletion anywhere: the maximum destructive action is an atomic
  move into the owner-only archive root on the same filesystem, and every
  archived skill can be restored. Backup pruning follows an explicit retention
  cap and never touches the newest ``keep`` snapshots.
- Telemetry is body-free: counters, ISO timestamps and state flags only. Bump
  is fail-open (telemetry failure never blocks a foreground skill call);
  lifecycle mutations are fail-closed when telemetry/provenance is unreadable.
- LLM consolidation is not implemented. Setting CCC_SKILL_CURATOR_CONSOLIDATE
  makes ``run`` fail closed; this module never calls a provider.
- Clock/thresholds are configurable; CCC_SKILL_CURATOR_NOW pins the clock for
  deterministic tests (UTC only).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
from datetime import datetime, timedelta, timezone
from typing import Any
import uuid


def _load_ownership():
    path = Path(__file__).resolve().parent / "ownership.py"
    spec = importlib.util.spec_from_file_location("skill_ownership", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("ownership_module_unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ownership = _load_ownership()
ContractError = ownership.ContractError

_USAGE_FILE = "skill-autosave-usage.json"
_CURATOR_STATE_FILE = "skill-autosave-curator-state.json"
_ARCHIVE_DIR = "skill-autosave-archive"
_BACKUP_DIR = "skill-autosave-curator-backups"
_STAGING_DIR = ".curator-restore-staging"
_MAX_USAGE_BYTES = 4 * 1024 * 1024
_MAX_BACKUP_SKILL_BYTES = 8 * 1024 * 1024
_MAX_BACKUP_TOTAL_BYTES = 64 * 1024 * 1024
_ARCHIVE_NAME_RE = re.compile(
    r"^(?P<name>[a-z0-9]+(?:-[a-z0-9]+)*)\.(?P<ts>\d{14})\.(?P<rand>[0-9a-f]{8})$"
)
_BACKUP_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z(?:-\d{2})?$")
_ARCHIVE_MARKER = ".curator-archive.json"
_EVENTS = {"view", "use"}
_STATES = {"active", "stale", "archived"}
_CURATOR_TX_EVENTS = {"curator-archive", "curator-restore"}
_TERMINAL_OUTCOMES = {
    "archived",
    "archived-durability-uncertain",
    "restored",
    "restored-durability-uncertain",
    "aborted",
    "conflict",
}


# ---------------------------------------------------------------------------
# Configuration (bounded, UTC-only)
# ---------------------------------------------------------------------------


def _env_int(name: str, default: int, low: int, high: int) -> int:
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ContractError(f"invalid_config_{name}") from None
    if value < low or value > high:
        raise ContractError(f"invalid_config_{name}")
    return value


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "")
    if not raw:
        return default
    if raw.lower() in {"1", "true", "yes", "on"}:
        return True
    if raw.lower() in {"0", "false", "no", "off"}:
        return False
    raise ContractError(f"invalid_config_{name}")


def _load_config() -> dict[str, Any]:
    return {
        "enabled": _env_bool("CCC_SKILL_CURATOR_ENABLED", False),
        "stale_after_days": _env_int("CCC_SKILL_CURATOR_STALE_AFTER_DAYS", 30, 1, 3650),
        "archive_after_days": _env_int(
            "CCC_SKILL_CURATOR_ARCHIVE_AFTER_DAYS", 90, 1, 3650
        ),
        "min_idle_hours": _env_int("CCC_SKILL_CURATOR_MIN_IDLE_HOURS", 2, 0, 24 * 365),
        "interval_hours": _env_int("CCC_SKILL_CURATOR_INTERVAL_HOURS", 24, 1, 24 * 365),
        "backup_keep": _env_int("CCC_SKILL_CURATOR_BACKUP_KEEP", 5, 1, 100),
        "consolidate": _env_bool("CCC_SKILL_CURATOR_CONSOLIDATE", False),
    }


def _now() -> datetime:
    pinned = os.environ.get("CCC_SKILL_CURATOR_NOW", "")
    if pinned:
        try:
            parsed = datetime.fromisoformat(pinned.replace("Z", "+00:00"))
        except ValueError:
            raise ContractError("invalid_config_CCC_SKILL_CURATOR_NOW") from None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def _ts(value: datetime | None = None) -> str:
    return ownership._timestamp(value)


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Telemetry store (body-free)
# ---------------------------------------------------------------------------


def _empty_record(target_id: str, created_at: str) -> dict[str, Any]:
    return {
        "target_id": target_id,
        "view_count": 0,
        "use_count": 0,
        "patch_count": 0,
        "created_at": created_at,
        "last_viewed_at": None,
        "last_used_at": None,
        "last_patched_at": None,
        "state": "active",
        "archived_at": None,
        "archive_name": None,
    }


def _validate_usage(data: dict[str, Any]) -> dict[str, Any]:
    if (
        type(data.get("schema_version")) is not int
        or data["schema_version"] != 1
        or not isinstance(data.get("records"), dict)
    ):
        raise ContractError("usage_metadata_invalid")
    for key, record in data["records"].items():
        if not isinstance(key, str) or not isinstance(record, dict):
            raise ContractError("usage_metadata_invalid")
        if (
            not isinstance(record.get("target_id"), str)
            or type(record.get("view_count")) is not int
            or type(record.get("use_count")) is not int
            or type(record.get("patch_count")) is not int
            or record["view_count"] < 0
            or record["use_count"] < 0
            or record["patch_count"] < 0
            or not isinstance(record.get("created_at"), str)
            or record.get("state") not in _STATES
        ):
            raise ContractError("usage_metadata_invalid")
        for field in (
            "last_viewed_at",
            "last_used_at",
            "last_patched_at",
            "archived_at",
            "archive_name",
        ):
            if record.get(field) is not None and not isinstance(record.get(field), str):
                raise ContractError("usage_metadata_invalid")
    return data


def _load_usage(context, *, strict: bool) -> dict[str, Any]:
    path = context.state_dir / _USAGE_FILE
    try:
        data = ownership._safe_json_file(
            path, owner=context.uid, max_bytes=_MAX_USAGE_BYTES
        )
    except FileNotFoundError:
        return {"schema_version": 1, "records": {}}
    except ContractError:
        if strict:
            raise
        return {"schema_version": 1, "records": {}}
    return _validate_usage(data)


def _save_usage(context, usage: dict[str, Any]) -> None:
    ownership._write_private_atomic(context.state_dir / _USAGE_FILE, usage, context)


def _record_key(context, name: str) -> str:
    return f"{context.provider}:{name}"


def _last_activity(record: dict[str, Any]) -> datetime | None:
    stamps = [
        _parse_ts(record.get("last_used_at")),
        _parse_ts(record.get("last_viewed_at")),
        _parse_ts(record.get("last_patched_at")),
    ]
    stamps = [stamp for stamp in stamps if stamp is not None]
    return max(stamps) if stamps else None


def _seed_record(context, usage: dict[str, Any], name: str, now: datetime) -> dict[str, Any]:
    """First-sight seeding: created_at prefers durable provenance, else now."""
    created = None
    marker_path = context.skills_dir / name / ownership._AUTOSAVE_MARKER
    try:
        marker = ownership._safe_json_file(marker_path, owner=context.uid)
        if marker.get("schema_version") == 2:
            created = _parse_ts(marker.get("created_at"))
    except (ContractError, FileNotFoundError):
        created = None
    if created is None:
        for row in ownership._read_ledger(context):
            if (
                row.get("event") in {"create", "adopt"}
                and row.get("outcome") == "changed"
                and row.get("provider") == context.provider
                and row.get("name") == name
            ):
                created = _parse_ts(row.get("ts")) or created
    record = _empty_record(
        ownership._target_id(context, name), _ts(created or now)
    )
    usage["records"][_record_key(context, name)] = record
    return record


def _sync_patches_from_ledger(context, usage: dict[str, Any]) -> None:
    """Recompute patch_count/last_patched_at from the ownership ledger.

    Idempotent: counts are recomputed from source-of-truth rows each time.
    """
    applied: dict[str, dict[str, Any]] = {}
    for row in ownership._read_ledger(context):
        if (
            row.get("event") == "skill-proposal-apply"
            and row.get("outcome") == "applied"
            and row.get("provider") == context.provider
            and isinstance(row.get("name"), str)
        ):
            key = _record_key(context, row["name"])
            entry = applied.setdefault(key, {"count": 0, "latest": None})
            entry["count"] += 1
            stamp = _parse_ts(row.get("ts"))
            if stamp is not None and (
                entry["latest"] is None or stamp > entry["latest"]
            ):
                entry["latest"] = stamp
    for key, entry in applied.items():
        record = usage["records"].get(key)
        if record is None:
            continue
        record["patch_count"] = entry["count"]
        if entry["latest"] is not None:
            record["last_patched_at"] = _ts(entry["latest"])


# ---------------------------------------------------------------------------
# Curator run state (interval gating for --auto)
# ---------------------------------------------------------------------------


def _load_curator_state(context) -> dict[str, Any]:
    path = context.state_dir / _CURATOR_STATE_FILE
    try:
        data = ownership._safe_json_file(path, owner=context.uid)
    except FileNotFoundError:
        return {"schema_version": 1, "last_run_at": None, "run_count": 0}
    if (
        type(data.get("schema_version")) is not int
        or data["schema_version"] != 1
        or type(data.get("run_count")) is not int
        or data["run_count"] < 0
        or (data.get("last_run_at") is not None and not isinstance(data.get("last_run_at"), str))
    ):
        raise ContractError("curator_state_invalid")
    return data


def _save_curator_state(context, state: dict[str, Any]) -> None:
    ownership._write_private_atomic(
        context.state_dir / _CURATOR_STATE_FILE, state, context
    )


# ---------------------------------------------------------------------------
# Archive root helpers
# ---------------------------------------------------------------------------


def _archive_archive_name(name: str, now: datetime) -> str:
    return f"{name}.{now.strftime('%Y%m%d%H%M%S')}.{uuid.uuid4().hex[:8]}"


def _check_same_filesystem(context) -> None:
    ownership._ensure_private_dir(context.state_dir / _ARCHIVE_DIR, context)
    skills_meta = ownership._lstat(context.skills_dir)
    archive_meta = ownership._lstat(context.state_dir / _ARCHIVE_DIR)
    if skills_meta is None or archive_meta is None:
        raise ContractError("archive_root_missing")
    if skills_meta.st_dev != archive_meta.st_dev:
        raise ContractError("archive_cross_device")


def _list_archive_entries(context) -> list[dict[str, Any]]:
    root = context.state_dir / _ARCHIVE_DIR
    metadata = ownership._lstat(root)
    if metadata is None:
        return []
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise ContractError("unsafe_archive_root")
    entries: list[dict[str, Any]] = []
    try:
        for entry in os.scandir(root):
            match = _ARCHIVE_NAME_RE.fullmatch(entry.name)
            if match is None:
                continue
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError:
                raise ContractError("unsafe_archive_entry") from None
            if not stat.S_ISDIR(entry_stat.st_mode) or stat.S_ISLNK(
                entry_stat.st_mode
            ):
                continue
            entries.append(
                {
                    "archive_name": entry.name,
                    "name": match.group("name"),
                    "archived_at": datetime.strptime(
                        match.group("ts"), "%Y%m%d%H%M%S"
                    ).replace(tzinfo=timezone.utc),
                }
            )
    except OSError:
        raise ContractError("archive_root_unreadable") from None
    return sorted(entries, key=lambda item: item["archive_name"])


# ---------------------------------------------------------------------------
# Crash recovery for prepared-but-unterminated curator transactions
# ---------------------------------------------------------------------------


def _recovery_outcome(event: str, live: bool, archived: bool) -> str:
    """Map observed FS state to a terminal outcome for a dangling prepared row."""
    if event == "curator-archive":
        if archived and not live:
            return "archived"
        if live and not archived:
            return "aborted"
        return "conflict"
    if live and not archived:
        return "restored"
    if archived and not live:
        return "aborted"
    return "conflict"


def _apply_recovery_to_record(
    record: dict[str, Any] | None,
    event: str,
    outcome: str,
    archive_name: str,
    fields: dict[str, Any],
) -> None:
    if record is None or outcome == "conflict":
        return
    if event == "curator-archive":
        if outcome == "archived":
            record["state"] = "archived"
            record["archived_at"] = fields.get("archived_at") or record["archived_at"]
            record["archive_name"] = archive_name
            return
        record["state"] = "active"
        record["archived_at"] = None
        record["archive_name"] = None
        return
    if outcome == "restored":
        record["state"] = "active"
        record["archived_at"] = None
        record["archive_name"] = None
        return
    record["state"] = "archived"
    record["archive_name"] = archive_name


def _recover_one_transaction(
    context, usage: dict[str, Any], tx: str, row: dict[str, Any]
) -> dict[str, Any]:
    """Finish one dangling prepared row; appends exactly one terminal row."""
    event = row["event"]
    name = row.get("name")
    archive_name = row.get("archive_name")
    if (
        row.get("provider") != context.provider
        or not isinstance(name, str)
        or not ownership._NAME_RE.fullmatch(name)
        or not isinstance(archive_name, str)
        or _ARCHIVE_NAME_RE.fullmatch(archive_name) is None
    ):
        ownership._append_ledger(
            context,
            ownership._transaction_record(
                event, tx, outcome="conflict", fields={"reason": "prepared_row_invalid"}
            ),
        )
        return {"transaction_id": tx, "event": event, "outcome": "conflict"}
    live = ownership._lstat(context.skills_dir / name) is not None
    archived = (
        ownership._lstat(context.state_dir / _ARCHIVE_DIR / archive_name) is not None
    )
    outcome = _recovery_outcome(event, live, archived)
    fields = ownership._transaction_fields_from_record(row)
    ownership._append_ledger(
        context,
        ownership._transaction_record(event, tx, outcome=outcome, fields=fields),
    )
    _apply_recovery_to_record(
        usage["records"].get(_record_key(context, name)),
        event,
        outcome,
        archive_name,
        fields,
    )
    return {"transaction_id": tx, "event": event, "name": name, "outcome": outcome}


def _recover_curator_transactions(context, usage: dict[str, Any]) -> list[dict[str, Any]]:
    """Reconcile prepared curator rows without a terminal row.

    Idempotent: a terminal row is appended for every dangling prepared row,
    so a later crash simply re-derives the same terminal state.
    """
    prepared: dict[str, dict[str, Any]] = {}
    terminated: set[str] = set()
    for row in ownership._read_ledger(context):
        if row.get("event") not in _CURATOR_TX_EVENTS:
            continue
        tx = row.get("transaction_id")
        if not isinstance(tx, str):
            continue
        if row.get("outcome") == "prepared":
            prepared[tx] = row
        elif row.get("outcome") in _TERMINAL_OUTCOMES:
            terminated.add(tx)
    recoveries: list[dict[str, Any]] = []
    for tx, row in sorted(prepared.items()):
        if tx in terminated:
            continue
        recoveries.append(_recover_one_transaction(context, usage, tx, row))
    if recoveries:
        # Recovery owns its usage mutations: persist immediately so a later
        # failure in the calling command cannot drop a reconciled state.
        _save_usage(context, usage)
    return recoveries


# ---------------------------------------------------------------------------
# Archive / restore primitives (atomic same-filesystem moves)
# ---------------------------------------------------------------------------


def _move_directory(src_root_fd: int, dst_root_fd: int, src: str, dst: str) -> bool:
    """os.rename between dir_fds with fsync; returns durability certainty."""
    os.rename(src, dst, src_dir_fd=src_root_fd, dst_dir_fd=dst_root_fd)
    durable = True
    try:
        os.fsync(src_root_fd)
        os.fsync(dst_root_fd)
    except OSError:
        durable = False
    return durable


def _open_dir(path: Path) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_DIRECTORY", 0)
    )
    return os.open(path, flags)


def _classify_for_lifecycle(context, name: str) -> dict[str, Any]:
    record = ownership._classification(context, name)
    if record["base_classification"] != "autosave-managed":
        raise ContractError(
            f"lifecycle_denied_{record['base_classification'].replace('/', '_')}"
        )
    if record["pinned"]:
        raise ContractError("lifecycle_denied_pinned")
    return record


def _archive_skill(context, usage, name: str, now: datetime, *, manual: bool) -> dict[str, Any]:
    classification = _classify_for_lifecycle(context, name)
    key = _record_key(context, name)
    record = usage["records"].get(key)
    if record is None:
        raise ContractError("lifecycle_denied_unseeded")
    if record["state"] == "archived":
        return {"changed": False, "reason": "already-archived", "name": name}
    ownership._ensure_private_dir(context.state_dir / _ARCHIVE_DIR, context)
    _check_same_filesystem(context)
    archive_name = _archive_archive_name(name, now)
    tx = uuid.uuid4().hex
    fields = {
        "provider": context.provider,
        "name": name,
        "target_id": classification["target_id"],
        "archive_name": archive_name,
        "archived_at": _ts(now),
        "trigger": "manual" if manual else "automatic",
        "skill_sha256": classification["skill_sha256"],
    }
    ownership._append_ledger(
        context,
        ownership._transaction_record(
            "curator-archive", tx, outcome="prepared", fields=fields
        ),
    )
    skills_fd: int | None = None
    archive_fd: int | None = None
    renamed = False
    durable = True
    try:
        skills_fd = _open_dir(context.skills_dir)
        archive_fd = _open_dir(context.state_dir / _ARCHIVE_DIR)
        source = os.stat(name, dir_fd=skills_fd, follow_symlinks=False)
        if (
            not stat.S_ISDIR(source.st_mode)
            or source.st_uid != context.uid
            or stat.S_IMODE(source.st_mode) & 0o022
        ):
            raise ContractError("archive_source_changed")
        try:
            os.stat(archive_name, dir_fd=archive_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise ContractError("archive_entry_exists")
        durable = _move_directory(skills_fd, archive_fd, name, archive_name)
        renamed = True
        marker = {
            "schema_version": 1,
            "name": name,
            "provider": context.provider,
            "target_id": classification["target_id"],
            "archived_at": _ts(now),
            "skill_sha256": classification["skill_sha256"],
        }
        try:
            ownership._write_private_atomic(
                context.state_dir / _ARCHIVE_DIR / archive_name / _ARCHIVE_MARKER,
                marker,
                context,
            )
        except ContractError:
            durable = False
    except ContractError:
        ownership._finish_transaction(
            context, "curator-archive", tx, outcome="aborted", fields=fields
        )
        raise
    except OSError:
        ownership._finish_transaction(
            context, "curator-archive", tx, outcome="aborted", fields=fields
        )
        raise ContractError("archive_move_failed") from None
    finally:
        if skills_fd is not None:
            os.close(skills_fd)
        if archive_fd is not None:
            os.close(archive_fd)
    if not renamed:
        raise ContractError("archive_move_failed")
    record["state"] = "archived"
    record["archived_at"] = _ts(now)
    record["archive_name"] = archive_name
    _save_usage(context, usage)
    ownership._finish_transaction(
        context,
        "curator-archive",
        tx,
        outcome="archived" if durable else "archived-durability-uncertain",
        fields=fields,
    )
    return {
        "changed": True,
        "name": name,
        "archive_name": archive_name,
        "durable": durable,
    }


def _resolve_restore_target(
    context, usage: dict[str, Any], name: str
) -> tuple[dict[str, Any], str]:
    """Fail-closed validation: returns (record, archive_name) to restore."""
    ownership._validate_name(name)
    record = usage["records"].get(_record_key(context, name))
    entries = [
        entry for entry in _list_archive_entries(context) if entry["name"] == name
    ]
    if record is None or record["state"] != "archived":
        if entries:
            raise ContractError("restore_denied_record_missing")
        raise ContractError("restore_denied_not_archived")
    archive_name = record.get("archive_name")
    if (
        not isinstance(archive_name, str)
        or _ARCHIVE_NAME_RE.fullmatch(archive_name) is None
    ):
        raise ContractError("restore_denied_archive_unknown")
    matches = [entry for entry in entries if entry["archive_name"] == archive_name]
    if len(matches) != 1:
        raise ContractError("restore_denied_archive_missing")
    ownership._ensure_private_dir(context.state_dir / _ARCHIVE_DIR, context)
    _check_same_filesystem(context)
    if ownership._lstat(context.skills_dir / name) is not None:
        raise ContractError("restore_denied_live_exists")
    return record, archive_name


def _move_restored(context, name: str, archive_name: str) -> bool:
    """Atomic archive→live move + marker cleanup; returns durability certainty."""
    skills_fd: int | None = None
    archive_fd: int | None = None
    try:
        skills_fd = _open_dir(context.skills_dir)
        archive_fd = _open_dir(context.state_dir / _ARCHIVE_DIR)
        source = os.stat(archive_name, dir_fd=archive_fd, follow_symlinks=False)
        if not stat.S_ISDIR(source.st_mode) or source.st_uid != context.uid:
            raise ContractError("restore_source_changed")
        try:
            os.stat(name, dir_fd=skills_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise ContractError("restore_denied_live_exists")
        durable = _move_directory(archive_fd, skills_fd, archive_name, name)
        restored_fd: int | None = None
        try:
            restored_fd = _open_dir(context.skills_dir / name)
            os.unlink(_ARCHIVE_MARKER, dir_fd=restored_fd)
        except FileNotFoundError:
            pass
        except OSError:
            durable = False
        finally:
            if restored_fd is not None:
                os.close(restored_fd)
        return durable
    except OSError:
        raise ContractError("restore_move_failed") from None
    finally:
        if skills_fd is not None:
            os.close(skills_fd)
        if archive_fd is not None:
            os.close(archive_fd)


def _restore_skill(context, usage, name: str, now: datetime) -> dict[str, Any]:
    record, archive_name = _resolve_restore_target(context, usage, name)
    tx = uuid.uuid4().hex
    fields = {
        "provider": context.provider,
        "name": name,
        "target_id": record["target_id"],
        "archive_name": archive_name,
        "restored_at": _ts(now),
    }
    ownership._append_ledger(
        context,
        ownership._transaction_record(
            "curator-restore", tx, outcome="prepared", fields=fields
        ),
    )
    try:
        durable = _move_restored(context, name, archive_name)
    except ContractError:
        ownership._finish_transaction(
            context, "curator-restore", tx, outcome="aborted", fields=fields
        )
        raise
    record["state"] = "active"
    record["archived_at"] = None
    record["archive_name"] = None
    _save_usage(context, usage)
    ownership._finish_transaction(
        context,
        "curator-restore",
        tx,
        outcome="restored" if durable else "restored-durability-uncertain",
        fields=fields,
    )
    return {"changed": True, "name": name, "durable": durable}


# ---------------------------------------------------------------------------
# Backup / rollback (bounded snapshots, retention cap, fail-closed)
# ---------------------------------------------------------------------------


def _backup_id(now: datetime) -> str:
    return now.strftime("%Y-%m-%dT%H-%M-%SZ")


def _copy_skill_into_backup(context, name: str, dest_root: Path) -> dict[str, Any]:
    source = context.skills_dir / name
    total = 0
    file_count = 0
    for root, _dirs, files in os.walk(source, followlinks=False):
        for filename in files:
            path = Path(root) / filename
            metadata = ownership._lstat(path)
            if metadata is None or not stat.S_ISREG(metadata.st_mode):
                raise ContractError("backup_unsafe_member")
            total += metadata.st_size
            file_count += 1
            if metadata.st_size > _MAX_BACKUP_SKILL_BYTES:
                raise ContractError("backup_member_too_large")
        if total > _MAX_BACKUP_TOTAL_BYTES:
            raise ContractError("backup_total_too_large")
    destination = dest_root / name
    try:
        shutil.copytree(source, destination, symlinks=False)
    except OSError:
        raise ContractError("backup_copy_failed") from None
    return {"name": name, "file_count": file_count, "bytes": total}


def _prune_backups(context, keep: int, protect_ids: set[str]) -> list[str]:
    root = context.state_dir / _BACKUP_DIR
    metadata = ownership._lstat(root)
    if metadata is None:
        return []
    try:
        entries = sorted(
            entry.name
            for entry in os.scandir(root)
            if _BACKUP_ID_RE.fullmatch(entry.name)
            and stat.S_ISDIR(entry.stat(follow_symlinks=False).st_mode)
        )
    except OSError:
        raise ContractError("backup_root_unreadable") from None
    survivors = entries[-keep:] if len(entries) > keep else entries
    survivor_set = set(survivors) | set(protect_ids)
    pruned: list[str] = []
    for entry in entries:
        if entry in survivor_set:
            continue
        try:
            shutil.rmtree(root / entry)
        except OSError:
            raise ContractError("backup_prune_failed") from None
        pruned.append(entry)
    return pruned


def _snapshot(
    context,
    usage: dict[str, Any],
    *,
    reason: str,
    now: datetime,
    keep: int,
    protect_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Owner-only backup of autosave-managed skills + curator metadata.

    Required before any mutating run/rollback: failure aborts the caller.
    """
    ownership._ensure_private_dir(context.state_dir / _BACKUP_DIR, context)
    _check_same_filesystem(context)
    root = context.state_dir / _BACKUP_DIR
    backup_id = _backup_id(now)
    suffix = 1
    candidate = backup_id
    while ownership._lstat(root / candidate) is not None:
        suffix += 1
        if suffix > 99:
            raise ContractError("backup_id_exhausted")
        candidate = f"{backup_id}-{suffix:02d}"
    backup_id = candidate
    dest = root / backup_id
    skills_meta: list[dict[str, Any]] = []
    try:
        os.mkdir(dest, mode=0o700)
        os.chmod(dest, 0o700)
        os.mkdir(dest / "skills", mode=0o700)
        total_bytes = 0
        for name in ownership._skill_names(context):
            try:
                classification = ownership._classification(context, name)
            except ContractError:
                continue
            if classification["base_classification"] != "autosave-managed":
                continue
            info = _copy_skill_into_backup(context, name, dest / "skills")
            total_bytes += info["bytes"]
            if total_bytes > _MAX_BACKUP_TOTAL_BYTES:
                raise ContractError("backup_total_too_large")
            info["target_id"] = classification["target_id"]
            info["skill_sha256"] = classification["skill_sha256"]
            skills_meta.append(info)
        usage_path = context.state_dir / _USAGE_FILE
        usage_sha = None
        if ownership._lstat(usage_path) is not None:
            payload = ownership._canonical_json(usage)
            usage_sha = ownership._sha256(payload)
            ownership._write_private_atomic(dest / "usage.json", usage, context)
        controls = ownership._load_controls(context)
        control_sha = ownership._sha256(ownership._canonical_json(controls))
        ownership._write_private_atomic(dest / "control.json", controls, context)
        manifest = {
            "schema_version": 1,
            "id": backup_id,
            "reason": reason,
            "created_at": _ts(now),
            "provider": context.provider,
            "skills": skills_meta,
            "skill_count": len(skills_meta),
            "total_bytes": total_bytes,
            "usage_sha256": usage_sha,
            "control_sha256": control_sha,
        }
        ownership._write_private_atomic(dest / "manifest.json", manifest, context)
    except (ContractError, OSError):
        shutil.rmtree(dest, ignore_errors=True)
        raise
    pruned = _prune_backups(context, keep, protect_ids={backup_id} | set(protect_ids or ()))
    return {
        "backup_id": backup_id,
        "skill_count": len(skills_meta),
        "total_bytes": total_bytes,
        "pruned": pruned,
    }


def _read_manifest(context, backup_id: str) -> dict[str, Any]:
    if _BACKUP_ID_RE.fullmatch(backup_id) is None:
        raise ContractError("backup_id_invalid")
    path = context.state_dir / _BACKUP_DIR / backup_id / "manifest.json"
    try:
        manifest = ownership._safe_json_file(path, owner=context.uid, exact_mode=0o600)
    except FileNotFoundError:
        raise ContractError("backup_missing") from None
    if (
        manifest.get("schema_version") != 1
        or manifest.get("id") != backup_id
        or manifest.get("provider") != context.provider
        or not isinstance(manifest.get("skills"), list)
        or not isinstance(manifest.get("created_at"), str)
    ):
        raise ContractError("backup_manifest_invalid")
    for skill in manifest["skills"]:
        if (
            not isinstance(skill, dict)
            or not isinstance(skill.get("name"), str)
            or ownership._NAME_RE.fullmatch(skill["name"]) is None
        ):
            raise ContractError("backup_manifest_invalid")
    return manifest


def _list_backups(context) -> list[dict[str, Any]]:
    root = context.state_dir / _BACKUP_DIR
    if ownership._lstat(root) is None:
        return []
    try:
        names = sorted(
            entry.name
            for entry in os.scandir(root)
            if _BACKUP_ID_RE.fullmatch(entry.name)
            and stat.S_ISDIR(entry.stat(follow_symlinks=False).st_mode)
        )
    except OSError:
        raise ContractError("backup_root_unreadable") from None
    backups: list[dict[str, Any]] = []
    for name in names:
        try:
            manifest = _read_manifest(context, name)
        except ContractError:
            backups.append({"id": name, "readable": False})
            continue
        backups.append(
            {
                "id": name,
                "readable": True,
                "created_at": manifest["created_at"],
                "reason": manifest.get("reason"),
                "skill_count": manifest.get("skill_count"),
                "total_bytes": manifest.get("total_bytes"),
            }
        )
    return backups


def _plan_rollback(context, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """One plan entry per manifest skill: keep / restore-content / restore-archived."""
    backup_created = _parse_ts(manifest["created_at"])
    plans: list[dict[str, Any]] = []
    for skill in manifest["skills"]:
        name = skill["name"]
        live = ownership._lstat(context.skills_dir / name) is not None
        archived_entries = [
            entry for entry in _list_archive_entries(context) if entry["name"] == name
        ]
        if live:
            try:
                current = ownership._read_target(context, name, "SKILL.md")
                drift = current.sha256 != skill["skill_sha256"]
            except ContractError:
                drift = True
            plans.append(
                {"name": name, "action": "restore-content" if drift else "keep"}
            )
            continue
        if archived_entries:
            latest = max(archived_entries, key=lambda entry: entry["archive_name"])
            if backup_created is None or latest["archived_at"] >= backup_created:
                plans.append(
                    {
                        "name": name,
                        "action": "restore-archived",
                        "archive_name": latest["archive_name"],
                    }
                )
            else:
                plans.append({"name": name, "action": "keep-archived"})
            continue
        plans.append({"name": name, "action": "restore-content"})
    return plans


def _rollback_restore_content(
    context, backup_root: Path, staging: Path, name: str
) -> None:
    """Swap a live (or missing) skill dir with its backup copy, staging the old."""
    source = backup_root / "skills" / name
    if ownership._lstat(source) is None:
        raise ContractError("backup_member_missing")
    staged_new = staging / f"new-{name}"
    shutil.copytree(source, staged_new, symlinks=False)
    live = ownership._lstat(context.skills_dir / name) is not None
    skills_fd = _open_dir(context.skills_dir)
    staging_fd = _open_dir(staging)
    try:
        if live:
            os.rename(
                name, f"old-{name}", src_dir_fd=skills_fd, dst_dir_fd=staging_fd
            )
        os.rename(
            f"new-{name}", name, src_dir_fd=staging_fd, dst_dir_fd=skills_fd
        )
        os.fsync(skills_fd)
        os.fsync(staging_fd)
    except OSError:
        raise ContractError("rollback_restore_failed") from None
    finally:
        os.close(skills_fd)
        os.close(staging_fd)


def _rollback_apply_plan(
    context, usage, backup_root: Path, staging: Path, plan: dict[str, Any], now: datetime
) -> None:
    name = plan["name"]
    if plan["action"] == "restore-archived":
        record = usage["records"].get(_record_key(context, name))
        if record is None:
            record = _seed_record(context, usage, name, now)
        record["state"] = "archived"
        record["archive_name"] = plan["archive_name"]
        _restore_skill(context, usage, name, now)
        return
    _rollback_restore_content(context, backup_root, staging, name)


def _rollback_restore_metadata(context, usage, backup_root: Path) -> None:
    """Restore usage/control metadata, keeping states this rollback just made live."""
    backup_usage_path = backup_root / "usage.json"
    if ownership._lstat(backup_usage_path) is not None:
        restored_usage = _validate_usage(
            ownership._safe_json_file(
                backup_usage_path, owner=context.uid, exact_mode=0o600
            )
        )
        for key, record in usage["records"].items():
            if key not in restored_usage["records"]:
                restored_usage["records"][key] = record
            elif (
                record.get("state") == "active"
                and restored_usage["records"][key].get("state") == "archived"
            ):
                restored_usage["records"][key] = record
        _save_usage(context, restored_usage)
        usage["records"] = restored_usage["records"]
    backup_control_path = backup_root / "control.json"
    if ownership._lstat(backup_control_path) is None:
        return
    control = ownership._safe_json_file(
        backup_control_path, owner=context.uid, exact_mode=0o600
    )
    if (
        type(control.get("schema_version")) is int
        and control["schema_version"] == 1
        and isinstance(control.get("records"), dict)
    ):
        ownership._write_private_atomic(
            context.state_dir / ownership._CONTROL_FILE, control, context
        )


def _rollback_backup(
    context,
    usage: dict[str, Any],
    backup_id: str | None,
    now: datetime,
    *,
    keep: int,
    dry_run: bool,
) -> dict[str, Any]:
    """Restore curator-owned state from a backup.

    Restores usage/control metadata, moves skills archived after the backup
    back to live, and restores content of live skills that drifted from the
    backup. Takes a safety snapshot first so the rollback is itself undoable.
    Never deletes anything.
    """
    backups = _list_backups(context)
    readable = [entry for entry in backups if entry.get("readable")]
    if not readable:
        raise ContractError("backup_missing")
    if backup_id is None:
        backup_id = readable[-1]["id"]
    manifest = _read_manifest(context, backup_id)
    backup_root = context.state_dir / _BACKUP_DIR / backup_id
    plans = _plan_rollback(context, manifest)
    actions = [plan for plan in plans if not plan["action"].startswith("keep")]
    if dry_run:
        return {
            "ok": True,
            "command": "rollback",
            "dry_run": True,
            "backup_id": backup_id,
            "planned": plans,
            "changed": False,
        }
    safety = _snapshot(
        context,
        usage,
        reason="pre-rollback",
        now=now,
        keep=keep,
        protect_ids={backup_id},
    )
    tx = uuid.uuid4().hex
    fields = {
        "provider": context.provider,
        "backup_id": backup_id,
        "safety_backup_id": safety["backup_id"],
        "planned": len(actions),
    }
    ownership._append_ledger(
        context,
        ownership._transaction_record(
            "curator-rollback", tx, outcome="prepared", fields=fields
        ),
    )
    staging = context.state_dir / _STAGING_DIR / tx
    results: list[dict[str, Any]] = []
    try:
        ownership._ensure_private_dir(context.state_dir / _STAGING_DIR, context)
        os.mkdir(staging, mode=0o700)
        for plan in actions:
            _rollback_apply_plan(context, usage, backup_root, staging, plan, now)
            results.append({"name": plan["name"], "action": plan["action"], "ok": True})
        _rollback_restore_metadata(context, usage, backup_root)
        shutil.rmtree(staging, ignore_errors=True)
    except ContractError:
        ownership._finish_transaction(
            context, "curator-rollback", tx, outcome="aborted", fields=fields
        )
        raise
    ownership._finish_transaction(
        context, "curator-rollback", tx, outcome="completed", fields=fields
    )
    return {
        "ok": True,
        "command": "rollback",
        "dry_run": False,
        "backup_id": backup_id,
        "safety_backup_id": safety["backup_id"],
        "results": results,
        "changed": bool(results),
    }


# ---------------------------------------------------------------------------
# Deterministic transitions
# ---------------------------------------------------------------------------


def _decide(
    record: dict[str, Any],
    now: datetime,
    config: dict[str, Any],
) -> tuple[str, str]:
    """Pure transition decision: (action, reason).

    Actions: keep, mark-stale, archive, reactivate. Mirrors the Hermes
    semantics: anchor = last activity or created_at; never-used skills younger
    than the stale window get a grace pass; stale skills with fresh activity
    reactivate.
    """
    stale_cutoff = now - timedelta(days=config["stale_after_days"])
    archive_cutoff = now - timedelta(days=config["archive_after_days"])
    state = record["state"]
    last_activity = _last_activity(record)
    anchor = last_activity or _parse_ts(record.get("created_at")) or now
    never_used = record["use_count"] == 0
    if state == "archived":
        return "keep", "already-archived"
    if never_used and anchor > stale_cutoff:
        if state == "stale":
            return "reactivate", "never-used-grace"
        return "keep", "never-used-grace"
    if anchor <= archive_cutoff:
        return "archive", f"idle>{config['archive_after_days']}d"
    if anchor <= stale_cutoff and state == "active":
        return "mark-stale", f"idle>{config['stale_after_days']}d"
    if anchor > stale_cutoff and state == "stale":
        return "reactivate", "fresh-activity"
    return "keep", "within-window"


def _node_recently_active(usage: dict[str, Any], now: datetime, min_idle_hours: int) -> bool:
    if min_idle_hours <= 0:
        return False
    floor = now - timedelta(hours=min_idle_hours)
    for record in usage["records"].values():
        activity = _last_activity(record)
        if activity is not None and activity > floor:
            return True
    return False


def _run_auto_skip(
    context, state: dict[str, Any], config: dict[str, Any], now: datetime, dry_run: bool
) -> dict[str, Any] | None:
    """Auto-mode gating: returns a skip report, or None when the run proceeds."""
    base = {"ok": True, "command": "run", "auto": True, "changed": False}
    if not config["enabled"]:
        return {**base, "skipped": "curator-disabled"}
    last_run = _parse_ts(state.get("last_run_at"))
    if last_run is None:
        # First auto run only seeds the interval timer — never mutates a
        # library it has never seen (same safety as Hermes should_run_now).
        if not dry_run:
            state["last_run_at"] = _ts(now)
            _save_curator_state(context, state)
        return {**base, "skipped": "first-run-deferred"}
    if now - last_run < timedelta(hours=config["interval_hours"]):
        return {**base, "skipped": "interval-not-elapsed"}
    return None


def _run_report_skeleton(auto: bool, dry_run: bool, now: datetime, config) -> dict[str, Any]:
    return {
        "ok": True,
        "command": "run",
        "auto": auto,
        "dry_run": dry_run,
        "now": _ts(now),
        "config": {
            "stale_after_days": config["stale_after_days"],
            "archive_after_days": config["archive_after_days"],
            "min_idle_hours": config["min_idle_hours"],
        },
        "decisions": [],
        "counts": {
            "checked": 0,
            "seeded": 0,
            "protected": 0,
            "marked_stale": 0,
            "archived": 0,
            "reactivated": 0,
            "kept": 0,
        },
        "changed": False,
    }


def _classify_run_decision(
    context, usage, name: str, now: datetime, config, report, dry_run: bool
) -> None:
    """Append one skill's transition decision to the run report (no mutation)."""
    report["counts"]["checked"] += 1
    classification = ownership._classification(context, name)
    if classification["base_classification"] != "autosave-managed":
        report["counts"]["protected"] += 1
        report["decisions"].append(
            {
                "name": name,
                "action": "protect",
                "reason": classification["base_classification"],
            }
        )
        return
    if classification["pinned"]:
        report["counts"]["protected"] += 1
        report["decisions"].append({"name": name, "action": "protect", "reason": "pinned"})
        return
    record = usage["records"].get(_record_key(context, name))
    if record is None:
        if not dry_run:
            _seed_record(context, usage, name, now)
        report["counts"]["seeded"] += 1
        report["decisions"].append({"name": name, "action": "seed", "reason": "first-sight"})
        return
    action, reason = _decide(record, now, config)
    report["decisions"].append({"name": name, "action": action, "reason": reason})
    count_keys = {
        "keep": "kept",
        "mark-stale": "marked_stale",
        "reactivate": "reactivated",
        "archive": "archived",
    }
    report["counts"][count_keys[action]] += 1


def _apply_run_decisions(context, usage, report, now: datetime, config) -> None:
    """Backup first, then apply every planned transition (lock already held)."""
    backup = _snapshot(
        context, usage, reason="pre-curator-run", now=now, keep=config["backup_keep"]
    )
    report["backup"] = backup
    for decision in report["decisions"]:
        action = decision["action"]
        if action not in {"mark-stale", "reactivate", "archive"}:
            continue
        record = usage["records"][_record_key(context, decision["name"])]
        if action == "mark-stale":
            record["state"] = "stale"
        elif action == "reactivate":
            record["state"] = "active"
        elif action == "archive" and record["state"] != "archived":
            result = _archive_skill(context, usage, decision["name"], now, manual=False)
            decision["archive_name"] = result.get("archive_name")
            decision["durable"] = result.get("durable")
    report["changed"] = True
    _save_usage(context, usage)


def _command_run(context, *, dry_run: bool, auto: bool) -> dict[str, Any]:
    config = _load_config()
    if config["consolidate"]:
        # Phase-3 LLM consolidation is not implemented; never call a provider.
        raise ContractError("consolidation_not_implemented")
    now = _now()
    state = _load_curator_state(context)
    if auto:
        skip = _run_auto_skip(context, state, config, now, dry_run)
        if skip is not None:
            return skip
    usage = _load_usage(context, strict=True)
    report = _run_report_skeleton(auto, dry_run, now, config)
    with ownership._MutationLock(context):
        recoveries = _recover_curator_transactions(context, usage)
        if recoveries:
            report["recoveries"] = recoveries
        _sync_patches_from_ledger(context, usage)
        if auto and _node_recently_active(usage, now, config["min_idle_hours"]):
            _save_usage(context, usage)
            report["skipped"] = "node-active-within-min-idle"
            return report
        for name in ownership._skill_names(context):
            _classify_run_decision(context, usage, name, now, config, report, dry_run)
        planned = any(
            decision["action"] in {"mark-stale", "reactivate", "archive"}
            for decision in report["decisions"]
        )
        if not dry_run and planned:
            _apply_run_decisions(context, usage, report, now, config)
        elif not dry_run:
            _save_usage(context, usage)
        if not dry_run:
            state["last_run_at"] = _ts(now)
            state["run_count"] += 1
            _save_curator_state(context, state)
    return report


# ---------------------------------------------------------------------------
# Reports (body-free)
# ---------------------------------------------------------------------------


def _idle_days(record: dict[str, Any], now: datetime) -> int | None:
    anchor = _last_activity(record) or _parse_ts(record.get("created_at"))
    if anchor is None:
        return None
    return max(0, (now - anchor).days)


def _skill_report(context, name, classification, usage, now) -> dict[str, Any]:
    key = _record_key(context, name)
    record = usage["records"].get(key)
    entry: dict[str, Any] = {
        "name": name,
        "classification": classification["classification"],
        "base_classification": classification["base_classification"],
        "pinned": classification["pinned"],
        "skill_sha256": classification.get("skill_sha256"),
        "provenance_revision": classification.get("provenance_revision"),
    }
    if record is None:
        entry["telemetry"] = None
        return entry
    entry["telemetry"] = {
        "state": record["state"],
        "view_count": record["view_count"],
        "use_count": record["use_count"],
        "patch_count": record["patch_count"],
        "created_at": record["created_at"],
        "last_viewed_at": record["last_viewed_at"],
        "last_used_at": record["last_used_at"],
        "last_patched_at": record["last_patched_at"],
        "last_activity_at": (
            _ts(activity) if (activity := _last_activity(record)) else None
        ),
        "idle_days": _idle_days(record, now),
        "archived_at": record["archived_at"],
    }
    return entry


def _command_status(context, name: str | None) -> dict[str, Any]:
    now = _now()
    usage = _load_usage(context, strict=False)
    names = [name] if name else ownership._skill_names(context)
    skills = []
    for item in names:
        classification = ownership._classification(context, item)
        skills.append(_skill_report(context, item, classification, usage, now))
    archived = _list_archive_entries(context)
    return {
        "ok": True,
        "command": "status",
        "now": _ts(now),
        "skills": skills,
        "archived_count": len(archived),
    }


def _command_report(context) -> dict[str, Any]:
    now = _now()
    usage = _load_usage(context, strict=False)
    by_state: dict[str, int] = {"active": 0, "stale": 0, "archived": 0, "untracked": 0}
    by_class: dict[str, int] = {}
    skills = []
    for name in ownership._skill_names(context):
        classification = ownership._classification(context, name)
        by_class[classification["base_classification"]] = (
            by_class.get(classification["base_classification"], 0) + 1
        )
        record = usage["records"].get(_record_key(context, name))
        if record is None:
            by_state["untracked"] += 1
        else:
            by_state[record["state"]] += 1
        skills.append(_skill_report(context, name, classification, usage, now))
    recent = sorted(
        (
            {
                "name": skill["name"],
                "last_activity_at": (skill.get("telemetry") or {}).get(
                    "last_activity_at"
                ),
            }
            for skill in skills
            if (skill.get("telemetry") or {}).get("last_activity_at")
        ),
        key=lambda item: item["last_activity_at"],
        reverse=True,
    )[:5]
    state = None
    try:
        state = _load_curator_state(context)
    except ContractError:
        state = None
    return {
        "ok": True,
        "command": "report",
        "now": _ts(now),
        "totals": {
            "skills": len(skills),
            "by_state": by_state,
            "by_classification": by_class,
            "archived_dirs": len(_list_archive_entries(context)),
            "backups": len(_list_backups(context)),
        },
        "last_run_at": (state or {}).get("last_run_at"),
        "run_count": (state or {}).get("run_count", 0),
        "recent_activity": recent,
        "config": _load_config(),
    }


def _command_list_archived(context) -> dict[str, Any]:
    usage = _load_usage(context, strict=False)
    entries = []
    for entry in _list_archive_entries(context):
        record = usage["records"].get(_record_key(context, entry["name"]))
        entries.append(
            {
                "archive_name": entry["archive_name"],
                "name": entry["name"],
                "archived_at": _ts(entry["archived_at"]),
                "tracked": record is not None
                and record.get("archive_name") == entry["archive_name"],
            }
        )
    return {"ok": True, "command": "list-archived", "archived": entries}


# ---------------------------------------------------------------------------
# Remaining commands
# ---------------------------------------------------------------------------


def _command_bump(context, name: str, event: str) -> dict[str, Any]:
    """Fail-open telemetry increment; never blocks the foreground caller."""
    try:
        ownership._validate_name(name)
        if event not in _EVENTS:
            raise ContractError("invalid_event")
        if ownership._lstat(context.skills_dir / name / "SKILL.md") is None:
            raise ContractError("skill_missing")
        now = _now()
        with ownership._MutationLock(context):
            usage = _load_usage(context, strict=False)
            key = _record_key(context, name)
            record = usage["records"].get(key)
            if record is None:
                record = _empty_record(ownership._target_id(context, name), _ts(now))
                usage["records"][key] = record
            record[f"{event}_count"] += 1
            record[f"last_{'viewed' if event == 'view' else 'used'}_at"] = _ts(now)
            _save_usage(context, usage)
        return {"ok": True, "command": "bump", "recorded": True}
    except (ContractError, OSError):
        return {"ok": True, "command": "bump", "recorded": False, "degraded": True}


def _command_archive(context, name: str, dry_run: bool) -> dict[str, Any]:
    now = _now()
    classification = _classify_for_lifecycle(context, name)
    if dry_run:
        return {
            "ok": True,
            "command": "archive",
            "dry_run": True,
            "changed": False,
            "reason": "would-archive",
            "name": name,
            "target_id": classification["target_id"],
        }
    usage = _load_usage(context, strict=True)
    with ownership._MutationLock(context):
        _recover_curator_transactions(context, usage)
        _sync_patches_from_ledger(context, usage)
        if usage["records"].get(_record_key(context, name)) is None:
            _seed_record(context, usage, name, now)
        result = _archive_skill(context, usage, name, now, manual=True)
    return {"ok": True, "command": "archive", "dry_run": False, **result}


def _command_restore(context, name: str, dry_run: bool) -> dict[str, Any]:
    now = _now()
    if dry_run:
        usage = _load_usage(context, strict=False)
        record = usage["records"].get(_record_key(context, name)) or {}
        return {
            "ok": True,
            "command": "restore",
            "dry_run": True,
            "changed": False,
            "reason": "would-restore",
            "name": name,
            "archive_name": record.get("archive_name"),
        }
    usage = _load_usage(context, strict=True)
    with ownership._MutationLock(context):
        _recover_curator_transactions(context, usage)
        result = _restore_skill(context, usage, name, now)
    return {"ok": True, "command": "restore", "dry_run": False, **result}


def _command_backup(context, reason: str, dry_run: bool) -> dict[str, Any]:
    config = _load_config()
    now = _now()
    if dry_run:
        eligible = 0
        for name in ownership._skill_names(context):
            try:
                classification = ownership._classification(context, name)
            except ContractError:
                continue
            if classification["base_classification"] == "autosave-managed":
                eligible += 1
        return {
            "ok": True,
            "command": "backup",
            "dry_run": True,
            "changed": False,
            "eligible_skills": eligible,
            "keep": config["backup_keep"],
        }
    usage = _load_usage(context, strict=True)
    with ownership._MutationLock(context):
        result = _snapshot(
            context, usage, reason=reason, now=now, keep=config["backup_keep"]
        )
    return {"ok": True, "command": "backup", "dry_run": False, "changed": True, **result}


def _command_rollback(context, backup_id: str | None, dry_run: bool) -> dict[str, Any]:
    config = _load_config()
    now = _now()
    if backup_id is not None and _BACKUP_ID_RE.fullmatch(backup_id) is None:
        raise ContractError("backup_id_invalid")
    if dry_run:
        usage = _load_usage(context, strict=False)
        return _rollback_backup(
            context, usage, backup_id, now, keep=config["backup_keep"], dry_run=True
        )
    usage = _load_usage(context, strict=True)
    with ownership._MutationLock(context):
        _recover_curator_transactions(context, usage)
        return _rollback_backup(
            context, usage, backup_id, now, keep=config["backup_keep"], dry_run=False
        )


def _command_list_backups(context) -> dict[str, Any]:
    return {"ok": True, "command": "list-backups", "backups": _list_backups(context)}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=["claude", "codex"], default=None)
    parser.add_argument("--skills-dir", type=Path, default=None)
    parser.add_argument("--state-dir", type=Path, default=None)
    subparsers = parser.add_subparsers(dest="command", required=True)

    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("name", nargs="?", default=None)
    subparsers.add_parser("report")
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--dry-run", action="store_true")
    run_parser.add_argument("--auto", action="store_true")
    for command in ("pin", "unpin"):
        pin_parser = subparsers.add_parser(command)
        pin_parser.add_argument("name")
        pin_parser.add_argument("--dry-run", action="store_true")
    archive_parser = subparsers.add_parser("archive")
    archive_parser.add_argument("name")
    archive_parser.add_argument("--dry-run", action="store_true")
    restore_parser = subparsers.add_parser("restore")
    restore_parser.add_argument("name")
    restore_parser.add_argument("--dry-run", action="store_true")
    subparsers.add_parser("list-archived")
    backup_parser = subparsers.add_parser("backup")
    backup_parser.add_argument("--reason", default="manual")
    backup_parser.add_argument("--dry-run", action="store_true")
    rollback_parser = subparsers.add_parser("rollback")
    rollback_parser.add_argument("--id", dest="backup_id", default=None)
    rollback_parser.add_argument("--dry-run", action="store_true")
    subparsers.add_parser("list-backups")
    bump_parser = subparsers.add_parser("bump")
    bump_parser.add_argument("--event", choices=sorted(_EVENTS), required=True)
    bump_parser.add_argument("--name", required=True)
    return parser


def _dispatch(context, args) -> dict[str, Any]:
    handlers = {
        "status": lambda: _command_status(context, args.name),
        "report": lambda: _command_report(context),
        "run": lambda: _command_run(context, dry_run=args.dry_run, auto=args.auto),
        "pin": lambda: ownership._command_pin(context, args.name, True, args.dry_run),
        "unpin": lambda: ownership._command_pin(context, args.name, False, args.dry_run),
        "archive": lambda: _command_archive(context, args.name, args.dry_run),
        "restore": lambda: _command_restore(context, args.name, args.dry_run),
        "list-archived": lambda: _command_list_archived(context),
        "backup": lambda: _command_backup(context, args.reason, args.dry_run),
        "rollback": lambda: _command_rollback(context, args.backup_id, args.dry_run),
        "list-backups": lambda: _command_list_backups(context),
        "bump": lambda: _command_bump(context, args.name, args.event),
    }
    return handlers[args.command]()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        context = ownership._build_context(args)
        result = _dispatch(context, args)
    except ContractError as error:
        print(json.dumps({"ok": False, "code": error.code}, sort_keys=True))
        return 2
    except OSError:
        print(json.dumps({"ok": False, "code": "filesystem_error"}, sort_keys=True))
        return 2
    except (TypeError, ValueError, RecursionError):
        print(json.dumps({"ok": False, "code": "invalid_data"}, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

