#!/usr/bin/env python3
"""Thin ``ccc.distill.pending.v1`` adapter over the shared JSON journal core."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Mapping, Sequence


def _load_core() -> type[Any]:
    try:
        from telegram_bot.memory.journal_core import JsonJournalCore

        return JsonJournalCore
    except ModuleNotFoundError:
        hooks_root = Path(__file__).resolve().parent.parent
        installed = hooks_root / "ccc_journal_core.py"
        secure = hooks_root / "ccc_secure_fs.py"
        if not installed.is_file():
            repo_root = Path(__file__).resolve().parents[3]
            installed = repo_root / "bridge/memory/journal_core.py"
            secure = repo_root / "bridge/utils/secure_fs.py"
        for name, path in (("ccc_secure_fs", secure), ("ccc_journal_core", installed)):
            if name in sys.modules:
                continue
            spec = importlib.util.spec_from_file_location(name, path)
            if spec is None or spec.loader is None:
                raise RuntimeError("shared journal core unavailable")
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
        return sys.modules["ccc_journal_core"].JsonJournalCore


JsonJournalCore = _load_core()

SCHEMA = "ccc.distill.pending.v1"
SUCCESS_TOKEN = 76
HELD_EXIT = 75
INVALID_EXIT = 74
DEAD_EXIT = 73
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PRIVATE_SCOPE_RE = re.compile(r"^private-[0-9a-f]{32}$")
_ERROR_CLASS_RE = re.compile(r"^[a-z0-9_]{1,32}$")
_HARD_CLASSES = {
    "not_logged_in",
    "oauth_expired",
    "weekly_limit",
    "rate_limited",
}
_DISABLED = {"0", "false", "FALSE", "off", "OFF", "no", "NO"}
_MAX_TEXT = 4096
_ENV_FIELDS = (
    "isolation_profile",
    "wiki_memory_enabled",
    "memory_audience_scoped",
    "memory_audience",
    "memory_scope",
    "honcho_memory_enabled",
    "memory_user_label",
    "memory_assistant_label",
)
_IDENTITY_FIELDS = (
    "transcript_sha256",
    "session_id",
    "transcript_path",
    "source_cwd",
    "source_project",
    "dryrun",
    *_ENV_FIELDS,
)
_LEGACY_DEFAULTS: dict[str, object] = {
    "isolation_profile": "fleet",
    "wiki_memory_enabled": "1",
    "memory_audience_scoped": "0",
    "memory_audience": "legacy",
    "memory_scope": "",
    "honcho_memory_enabled": "1",
    "memory_user_label": "Seo Jin On / 서진원",
    "memory_assistant_label": "ccc-node assistant",
}


def _text(value: object, field: str, *, allow_empty: bool = True) -> str:
    if not isinstance(value, str) or len(value.encode("utf-8")) > _MAX_TEXT:
        raise ValueError(f"invalid_{field}")
    if not allow_empty and not value:
        raise ValueError(f"invalid_{field}")
    if "\0" in value or "\n" in value or "\r" in value:
        raise ValueError(f"invalid_{field}")
    return value


def _route(record: Mapping[str, Any]) -> tuple[bool, str, str]:
    scoped_raw = _text(record.get("memory_audience_scoped"), "memory_route")
    audience = _text(record.get("memory_audience"), "memory_route")
    scope = _text(record.get("memory_scope"), "memory_route")
    scoped = scoped_raw not in _DISABLED
    if scoped and not (
        (audience == "shared" and scope == "shared")
        or (audience == "private" and _PRIVATE_SCOPE_RE.fullmatch(scope))
    ):
        raise ValueError("invalid_memory_route")
    return scoped, audience, scope


def _parse_iso(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _in_backoff(record: Mapping[str, Any], now: datetime) -> bool:
    raw = record.get("retry_after")
    if raw in (None, ""):
        return False
    if not isinstance(raw, str):
        return True
    parsed = _parse_iso(raw)
    if parsed is None:
        return True
    return parsed > now


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    try:
        return int(raw)
    except ValueError:
        return default


def _max_attempts() -> int:
    # sogyo 2026-08-23: without a cap, jobs failing on a persistent provider
    # outage retried forever and the journal piled 0 -> 134 in 19h. Backoff
    # (#1253) spaces retries out; this cap ends them.
    value = _env_int("CCC_DISTILL_PENDING_MAX_ATTEMPTS", 5)
    return value if value > 0 else 5


def _max_age_hours() -> float:
    raw = os.environ.get("CCC_DISTILL_PENDING_MAX_AGE_HOURS", "")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 48.0
    return value if value > 0 else 48.0


def _record_age_hours(record: Mapping[str, Any]) -> float | None:
    """Age in hours, or None when created_at is missing/unparseable.

    Fail-open on purpose: a malformed timestamp must not dead-letter a
    processable job.
    """
    created = record.get("created_at")
    if not isinstance(created, str) or not created:
        return None
    stamp = _parse_iso(created)
    if stamp is None:
        return None
    return (datetime.now(timezone.utc) - stamp).total_seconds() / 3600.0


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _retry_after_iso(error_class: str, fail_count: int) -> str:
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    if error_class == "weekly_limit":
        delay_until = datetime(
            now.year, now.month, now.day, 1, 0, 0, tzinfo=timezone.utc
        )
        if delay_until <= now:
            delay_until = delay_until + timedelta(days=1)
    elif error_class == "rate_limited":
        delay_until = now + timedelta(
            seconds=max(_env_int("CCC_DISTILL_RATE_COOLDOWN_SEC", 1800), 1)
        )
    elif error_class in _HARD_CLASSES:
        delay_until = now + timedelta(
            seconds=max(_env_int("CCC_DISTILL_AUTH_COOLDOWN_SEC", 21600), 1)
        )
    else:
        base = max(_env_int("CCC_DISTILL_FAIL_BACKOFF_SEC", 900), 0)
        cap = max(_env_int("CCC_DISTILL_FAIL_BACKOFF_CAP_SEC", 14400), 0)
        shift = max(fail_count, 1) - 1
        delay_until = now + timedelta(seconds=min(base * (2**shift), cap))
    return delay_until.isoformat().replace("+00:00", "Z")


def _error_class_from_state(pending_root: Path) -> str:
    path = pending_root.parent / "distill-last-error.json"
    env_dir = os.environ.get("CCC_STATE_DIR")
    if env_dir:
        path = Path(env_dir) / "distill-last-error.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "extract_failed"
    klass = payload.get("class") if isinstance(payload, dict) else None
    if isinstance(klass, str) and _ERROR_CLASS_RE.fullmatch(klass):
        return klass
    return "extract_failed"


def _job_id(session_id: str, transcript_hash: str) -> str:
    material = b"\0".join(
        (session_id.encode("utf-8"), transcript_hash.encode("ascii"), b"v1")
    )
    return hashlib.sha256(material).hexdigest()


def _transcript_hash(path_value: str) -> str:
    path = Path(path_value)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    digest = hashlib.sha256()
    try:
        while chunk := os.read(fd, 65536):
            digest.update(chunk)
    finally:
        os.close(fd)
    return digest.hexdigest()


class PendingV1Journal(JsonJournalCore):
    """Legacy schema/CLI policy; durability remains in :class:`JsonJournalCore`."""

    def validate_record(self, record_id: str, value: Mapping[str, Any]) -> dict[str, Any]:
        record = dict(value)
        for field, default in _LEGACY_DEFAULTS.items():
            record.setdefault(field, default)
        if record.get("schema") != SCHEMA or record.get("job_id") != record_id:
            raise ValueError("invalid_identity")
        session_id = _text(record.get("session_id"), "identity", allow_empty=False)
        transcript_hash = _text(
            record.get("transcript_sha256"), "identity", allow_empty=False
        )
        if not _SHA256_RE.fullmatch(transcript_hash):
            raise ValueError("invalid_identity")
        if _job_id(session_id, transcript_hash) != record_id:
            raise ValueError("invalid_identity")
        for field in (
            "transcript_path",
            "source_cwd",
            "source_project",
            "trigger",
            "created_at",
            *_ENV_FIELDS,
        ):
            _text(
                record.get(field),
                field,
                allow_empty=field in {"source_cwd", "source_project", "memory_scope"},
            )
        if type(record.get("dryrun")) is not int or record["dryrun"] not in {0, 1}:
            raise ValueError("invalid_dryrun")
        if "retry_after" in record:
            _text(record.get("retry_after"), "retry_after")
        if "last_failed_at" in record:
            _text(record.get("last_failed_at"), "last_failed_at")
        if "last_error_class" in record:
            klass = record.get("last_error_class")
            if not isinstance(klass, str) or not _ERROR_CLASS_RE.fullmatch(klass):
                raise ValueError("invalid_last_error_class")
        if "fail_count" in record:
            count = record.get("fail_count")
            if type(count) is not int or count < 0 or count > 100000:
                raise ValueError("invalid_fail_count")
        _route(record)
        return record

    def read(self, record_id: str) -> dict[str, Any]:
        with self._exclusive():
            return self.validate_record(record_id, self._read_json_unlocked(record_id))

    def enqueue(self, value: Mapping[str, Any]) -> tuple[str, bool]:
        record_id = _text(value.get("job_id"), "identity", allow_empty=False)
        record = self.validate_record(record_id, value)
        with self._exclusive():
            path = self.record_path(record_id)
            if path.exists() or path.is_symlink():
                existing = self.validate_record(
                    record_id, self._read_json_unlocked(record_id)
                )
                if any(existing.get(field) != record.get(field) for field in _IDENTITY_FIELDS):
                    raise RuntimeError("metadata_collision")
                return record_id, False
            self._write_json_unlocked(record_id, record)
            return record_id, True

    def discover_claimable(self, limit: int) -> tuple[str, ...]:
        if limit <= 0:
            return ()
        now = datetime.now(timezone.utc)
        result: list[str] = []
        # Scan beyond held jobs, but do not parse or echo corrupt record bodies.
        for record_id in self.list_record_ids():
            if not self.is_claimable(record_id):
                continue
            try:
                record = self.read(record_id)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if _in_backoff(record, now):
                continue
            result.append(record_id)
            if len(result) == limit:
                break
        return tuple(result)

    def mark_retry(self, record_id: str, error_class: str) -> None:
        record = self.read(record_id)
        fail_count = int(record.get("fail_count") or 0) + 1
        record["fail_count"] = fail_count
        record["last_error_class"] = error_class
        record["last_failed_at"] = datetime.now(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
        record["retry_after"] = _retry_after_iso(error_class, fail_count)
        with self._exclusive():
            self._write_json_unlocked(
                record_id, self.validate_record(record_id, record)
            )

    def dead_letter_claimed(self, record_id: str) -> Path:
        """Move a claimed record out of the claimable set into ``dead/``.

        Dead-lettering is deliberate and loud (caller logs + exit code); the
        record body is preserved for postmortem instead of being silently
        dropped, mirroring the canary's no-silent-discard rule.
        """
        with self._exclusive():
            dead_dir = self.root / "dead"
            if dead_dir.is_symlink():
                raise ValueError("dead_letter_symlink")
            if not dead_dir.exists():
                dead_dir.mkdir(mode=0o700)
                os.chmod(dead_dir, 0o700)
            source = self.record_path(record_id)
            self._validate_regular_file(source)
            target = dead_dir / source.name
            if target.exists() or target.is_symlink():
                raise ValueError("dead_letter_collision")
            os.replace(source, target)
            os.chmod(target, 0o600)
            try:
                self.claim_path(record_id).unlink()
            except FileNotFoundError:
                pass
            _fsync_dir(dead_dir)
            _fsync_dir(self.root)
            return target


def _record_from_environment() -> dict[str, Any]:
    transcript_path = os.environ.get("CLAUDE_DISTILL_TRANSCRIPT", "")
    session_id = os.environ.get("CLAUDE_DISTILL_SESSION", "")
    transcript_hash = _transcript_hash(transcript_path)
    record_id = _job_id(session_id, transcript_hash)
    return {
        "schema": SCHEMA,
        "job_id": record_id,
        "transcript_sha256": transcript_hash,
        "session_id": session_id,
        "transcript_path": transcript_path,
        "source_cwd": os.environ.get("CLAUDE_DISTILL_SOURCE_CWD", ""),
        "source_project": os.environ.get("CLAUDE_DISTILL_SOURCE_PROJECT", ""),
        "trigger": os.environ.get("CLAUDE_DISTILL_TRIGGER", "manual"),
        "dryrun": int(os.environ.get("CLAUDE_DISTILL_DRYRUN", "0")),
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "isolation_profile": os.environ.get("CCC_NODE_ISOLATION_PROFILE", "fleet"),
        "wiki_memory_enabled": os.environ.get("CCC_WIKI_MEMORY_ENABLED", "1"),
        "memory_audience_scoped": os.environ.get("CCC_MEMORY_AUDIENCE_SCOPED", "0"),
        "memory_audience": os.environ.get("CCC_MEMORY_AUDIENCE", "legacy"),
        "memory_scope": os.environ.get("CCC_MEMORY_SCOPE", ""),
        "honcho_memory_enabled": os.environ.get("CCC_HONCHO_MEMORY_ENABLED", "1"),
        "memory_user_label": os.environ.get(
            "CCC_MEMORY_USER_LABEL", "Seo Jin On / 서진원"
        ),
        "memory_assistant_label": os.environ.get(
            "CCC_MEMORY_ASSISTANT_LABEL", "ccc-node assistant"
        ),
    }


def _child_environment(record: Mapping[str, Any]) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "CLAUDE_DISTILL_BG": "1",
            "CCC_PENDING_JOURNAL_MANAGED": "1",
            "CLAUDE_DISTILL_JOB": str(record["job_id"]),
            "CLAUDE_DISTILL_TRIGGER": str(record["trigger"]),
            "CLAUDE_DISTILL_SESSION": str(record["session_id"]),
            "CLAUDE_DISTILL_TRANSCRIPT": str(record["transcript_path"]),
            "CLAUDE_DISTILL_SOURCE_CWD": str(record["source_cwd"]),
            "CLAUDE_DISTILL_SOURCE_PROJECT": str(record["source_project"]),
            "CLAUDE_DISTILL_DRYRUN": str(record["dryrun"]),
            "CCC_NODE_ISOLATION_PROFILE": str(record["isolation_profile"]),
            "CCC_WIKI_MEMORY_ENABLED": str(record["wiki_memory_enabled"]),
            "CCC_HONCHO_MEMORY_ENABLED": str(record["honcho_memory_enabled"]),
            "CCC_MEMORY_USER_LABEL": str(record["memory_user_label"]),
            "CCC_MEMORY_ASSISTANT_LABEL": str(record["memory_assistant_label"]),
        }
    )
    env.pop("CLAUDE_DISTILL_INFLIGHT", None)
    scoped, audience, scope = _route(record)
    if scoped:
        inherited = (
            env.get("CCC_MEMORY_AUDIENCE_SCOPED"),
            env.get("CCC_MEMORY_AUDIENCE"),
            env.get("CCC_MEMORY_SCOPE"),
        )
        expected = (str(record["memory_audience_scoped"]), audience, scope)
        if any(value not in {None, expected[index]} for index, value in enumerate(inherited)):
            raise RuntimeError("memory_route_collision")
        env.update(
            {
                "CCC_MEMORY_AUDIENCE_SCOPED": expected[0],
                "CCC_MEMORY_AUDIENCE": audience,
                "CCC_MEMORY_SCOPE": scope,
            }
        )
    else:
        # Legacy jobs are deliberately unscoped; never inherit a caller route.
        env.pop("CCC_MEMORY_AUDIENCE_SCOPED", None)
        env.pop("CCC_MEMORY_AUDIENCE", None)
        env.pop("CCC_MEMORY_SCOPE", None)
    return env


def _journal(root: str) -> PendingV1Journal:
    journal = PendingV1Journal(Path(root))
    journal.initialize()
    return journal


def _enqueue(args: argparse.Namespace) -> int:
    record = _record_from_environment()
    record_id, created = _journal(args.root).enqueue(record)
    print(json.dumps({"job_id": record_id, "created": created}, separators=(",", ":")))
    return 0


def _discover(args: argparse.Namespace) -> int:
    journal = _journal(args.root)
    for record_id in journal.discover_claimable(args.limit):
        print(str(journal.record_path(record_id)))
    return 0


def _run(args: argparse.Namespace) -> int:
    journal = _journal(args.root)
    job_path = Path(args.job)
    if job_path.parent != journal.root or job_path.suffix != ".json":
        raise ValueError("path_outside_queue")
    record_id = job_path.stem
    with journal.claim_record(record_id) as claimed:
        if not claimed:
            return HELD_EXIT
        record = journal.read(record_id)
        age = _record_age_hours(record)
        if age is not None and age > _max_age_hours():
            journal.dead_letter_claimed(record_id)
            print("pending-journal: dead_lettered reason=ttl", file=sys.stderr)
            return DEAD_EXIT
        if _transcript_hash(str(record["transcript_path"])) != record["transcript_sha256"]:
            raise ValueError("transcript_changed")
        script = Path(args.script)
        if not script.is_absolute() or not script.is_file():
            raise ValueError("invalid_reentry_script")
        home = os.environ.get("HOME")
        stable_cwd = home if home and Path(home).is_dir() else "/"
        result = subprocess.run(
            ["bash", str(script), "recovery"],
            env=_child_environment(record),
            cwd=stable_cwd,
            check=False,
        )
        if result.returncode != SUCCESS_TOKEN:
            # fail_count+1 is what mark_retry would write; at the cap the job
            # dead-letters instead of earning yet another backoff cycle.
            if int(record.get("fail_count") or 0) + 1 >= _max_attempts():
                journal.dead_letter_claimed(record_id)
                print(
                    "pending-journal: dead_lettered reason=max_attempts",
                    file=sys.stderr,
                )
                return DEAD_EXIT
            journal.mark_retry(record_id, _error_class_from_state(journal.root))
            return 1
        journal.complete_claimed(record_id)
        return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    enqueue = sub.add_parser("enqueue")
    enqueue.add_argument("root")
    enqueue.set_defaults(func=_enqueue)
    discover = sub.add_parser("discover")
    discover.add_argument("root")
    discover.add_argument("--limit", type=int, default=3)
    discover.set_defaults(func=_discover)
    run = sub.add_parser("run")
    run.add_argument("root")
    run.add_argument("job")
    run.add_argument("script")
    run.set_defaults(func=_run)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as error:
        code = str(error) if str(error).replace("_", "").isalnum() else "invalid_job"
        print(f"pending-journal: {code}", file=sys.stderr)
        return INVALID_EXIT


if __name__ == "__main__":
    raise SystemExit(main())
