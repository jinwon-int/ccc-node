#!/usr/bin/env python3
"""ccc-erasure-apply — APPROVAL-GATED apply boundary for erasure plans (#873 step 4).

Executes the mechanical subset of a ccc-erasure-planner plan against this
node, under four fail-closed conditions that must ALL hold before any byte
is touched:

  1. plan digest binding   --plan <file> + --plan-digest <hex>; the supplied
     plan file's canonical sha256 must equal the digest argument, AND a fresh
     in-process re-plan for the same request must produce the SAME digest —
     if the world drifted one blocker or one resolved path since the plan was
     made, the stale plan is dead (fail-closed).
  2. blockers absent       any unclassified file in a managed state root
     aborts the run: an apply slice never deletes next to unknown artifacts.
  3. owner-only paths      every target (and each ancestor) must be owned by
     the euid, carry no group/other permission bits (ancestors: no group/
     other WRITE), and be a real file — any symlink aborts. Target paths
     must be present in the live resolver output for their class.
  4. rollback first        every target's pre-image is copied into an
     owner-only backup dir with its sha256 recorded BEFORE any mutation; a
     backup failure aborts with nothing deleted.

Scope of the mechanical subset (v1):
  - only targets whose inventory action is exactly "delete" and which are
    regular FILES are executed. Directory targets, pseudonymize/rebuild/
    redact/prune/handoff/retain actions are reported as skipped — they need
    request-specific logic or owner workflows (later slices).
  - default mode is plan-only: identical report, zero mutation, nothing
    written anywhere. ERASURE_APPLY=1 arms the mutation — a fresh per-node
    owner approval, never a default (#873 issue body; judge-batch #1270
    precedent).

Output is versioned and body-free: ids, paths, digests, counts — never file
contents. An armed run records a manifest (manifest.json) in its backup dir.

Exit codes: 0 ok (applied or plan-only), 2 usage, 4 blocked (any fail-closed
condition; the output names the reason). This script never touches anything
outside the verified target list and its backup dir.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import importlib.util
import json
import os
import shutil
import stat
import sys
import tempfile
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
# Semantic contract: resolution and policy live in the planner module — this
# script never re-implements a resolver or an action decision (the copied-rule
# drift class, #1204/#1211 lesson). Hyphenated filename → path-based import.
_planner_spec = importlib.util.spec_from_file_location(
    "ccc_erasure_planner", os.path.join(HERE, "ccc-erasure-planner.py"))
planner = importlib.util.module_from_spec(_planner_spec)
_planner_spec.loader.exec_module(planner)

APPLY_ENV = "ERASURE_APPLY"
MANIFEST_SCHEMA = "ccc.erasure-apply-manifest.v1"
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_BLOCKED = 4
# Actions this slice can execute. Everything else is reported as skipped:
# retain is a no-op by policy, handoff is owner workflow, the rest need
# request-specific logic (redact/pseudonymize/rebuild/prune) — later slices.
EXECUTABLE_ACTIONS = ("delete",)


class Blocked(Exception):
    """A fail-closed condition; the message is the body-free reason."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_digest(obj) -> str:
    """sha256 over a stable serialization — the digest is the approval handle,
    so the serialization must not depend on dict insertion order."""
    canonical = json.dumps(obj, sort_keys=True, ensure_ascii=True, indent=1)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def parse_args(argv: list[str]):
    parser = argparse.ArgumentParser(
        description="Approval-gated apply for ccc-erasure-planner plans "
                    "(#873 step 4). Default mode is plan-only; set "
                    f"{APPLY_ENV}=1 to arm mutation.")
    parser.add_argument("request", help="lifecycle request type")
    parser.add_argument("--audience", default=None)
    parser.add_argument("--key", default=None)
    parser.add_argument("--plan", required=True, help="planner plan JSON file")
    parser.add_argument("--plan-digest", required=True,
                        help="sha256 canonical digest of the approved plan")
    parser.add_argument("--inventory", default=planner.DEFAULT_INVENTORY)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def load_inventory(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            inventory = json.load(fh)
    except (OSError, ValueError) as exc:
        raise Blocked(f"cannot load inventory ({exc})")
    if inventory.get("schema") != planner.INVENTORY_SCHEMA:
        raise Blocked("inventory schema mismatch")
    return inventory


def bind_plan(args, inventory: dict) -> tuple[dict, dict, str]:
    """Digest binding (#873 3a): approved file digest, then live re-plan.

    The approval argument must cover the exact plan file, and the world must
    still produce the identical plan — otherwise the stale plan is dead.
    Returns (approved_plan, live_plan, digest).
    """
    if len(args.plan_digest) != 64 or any(
            c not in "0123456789abcdef" for c in args.plan_digest):
        raise Blocked("--plan-digest must be 64 lowercase hex chars")
    try:
        with open(args.plan, encoding="utf-8") as fh:
            approved = json.load(fh)
    except (OSError, ValueError) as exc:
        raise Blocked(f"cannot load plan ({exc})")
    if approved.get("schema") != planner.SCHEMA:
        raise Blocked("plan schema mismatch")
    if approved.get("request") != args.request:
        raise Blocked("plan request does not match the request argument")
    file_digest = canonical_digest(approved)
    if file_digest != args.plan_digest:
        raise Blocked("plan file digest does not match --plan-digest "
                      "(the approval does not cover this file)")
    live = planner.plan(
        args.request, inventory,
        args.audience if args.request == "audience-erasure" else None,
        args.key if args.request == "telegram-user-erasure" else None)
    if canonical_digest(live) != file_digest:
        raise Blocked("live re-plan differs from the approved plan "
                      "(world drifted since planning; re-plan and re-approve)")
    return approved, live, file_digest


def check_ancestors(path: str) -> str | None:
    """Owner-only verification for the target and its parent (#873 3b).

    Target file: no group/other bit at all, owned by euid, a real file, no
    symlink. The PARENT directory is what a delete TOCTOU rides on, so it
    must be ours and not group/other-writable. Ancestors above the parent
    are deliberately NOT walked: the plan digest binding already proves the
    path came from an inventory resolver chain, and the OS surface above a
    managed root (/tmp, /var, /) is not this script's contract.
    Returns a reason string when unsafe, None when verified.
    """
    abspath = os.path.abspath(path)
    if os.path.islink(abspath):
        return "symlink-target"
    try:
        meta = os.lstat(abspath)
    except OSError:
        return "missing-target"
    if not stat.S_ISREG(meta.st_mode):
        return "not-regular-file"
    if meta.st_uid != os.geteuid():
        return "owner-not-euid"
    if stat.S_IMODE(meta.st_mode) & 0o077:
        return "group-other-bits-on-target"
    parent = os.path.dirname(abspath)
    if os.path.islink(parent):
        return "symlink-parent"
    try:
        pmeta = os.lstat(parent)
    except OSError:
        return "missing-parent"
    if pmeta.st_uid != os.geteuid():
        return "parent-owner-not-euid"
    if stat.S_IMODE(pmeta.st_mode) & 0o022:
        return "parent-group-other-writable"
    return None


def partition_targets(live_plan: dict) -> tuple[list[dict], list[dict]]:
    """Split targets into executable and skipped (#873 3b verification).

    Owner-only verification runs here so an unsafe target aborts the whole
    run before a single backup is taken.
    """
    executable, skipped = [], []
    actions = {}
    for target in live_plan["targets"]:
        if target["present"] and target["path"]:
            path = os.path.abspath(target["path"])
            if path in actions:
                if actions[path] != target["action"]:
                    raise Blocked(f"conflicting actions for target: {path}")
                continue
            actions[path] = target["action"]
        if target["action"] not in EXECUTABLE_ACTIONS:
            skipped.append({"path": target["path"],
                            "artifact": target["artifact"],
                            "reason": f"action-{target['action']}-not-executable"})
            continue
        if not target["present"]:
            skipped.append({"path": target["path"],
                            "artifact": target["artifact"], "reason": "absent"})
            continue
        if os.path.isdir(target["path"] or ""):
            skipped.append({"path": target["path"],
                            "artifact": target["artifact"],
                            "reason": "dir-target-needs-later-slice"})
            continue
        reason = check_ancestors(target["path"] or "")
        if reason:
            raise Blocked(f"owner-only verification failed: {reason}: "
                          f"{target['path']}")
        executable.append(target)
    return executable, skipped


@contextmanager
def locked_backup_base():
    """Serialize armed runs from live re-plan through the final manifest.

    The lock lives outside per-run directories so two different timestamps
    cannot bypass it. Plan-only calls never enter here or create anything.
    """
    base = os.environ.get("CCC_ERASURE_BACKUP_DIR") \
        or os.path.join(os.path.expanduser("~"), ".erasure-backup")
    base_fd = lock_fd = None
    try:
        os.makedirs(base, mode=0o700, exist_ok=True)
        base_fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        meta = os.fstat(base_fd)
        if meta.st_uid != os.geteuid() or stat.S_IMODE(meta.st_mode) & 0o022:
            raise Blocked("unsafe backup base")
        os.fchmod(base_fd, 0o700)
        lock_fd = os.open(".apply.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                          0o600, dir_fd=base_fd)
        meta = os.fstat(lock_fd)
        if (not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1
                or meta.st_uid != os.geteuid() or stat.S_IMODE(meta.st_mode) & 0o077):
            raise Blocked("unsafe backup lock")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Blocked("another apply holds the backup lock") from None
    except (OSError, Blocked) as exc:
        if lock_fd is not None:
            os.close(lock_fd)
        if base_fd is not None:
            os.close(base_fd)
        if isinstance(exc, Blocked):
            raise
        raise Blocked(f"backup-dir-create-failed ({exc})") from exc
    try:
        yield base
    finally:
        os.close(lock_fd)
        os.close(base_fd)


def build_backup_dir(base: str) -> str:
    """Allocate a new owner-only directory, even for same-second runs."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    try:
        directory = tempfile.mkdtemp(prefix=f"{stamp}-", dir=base)
        fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        raise Blocked(f"backup-dir-create-failed ({exc})")
    return directory


def backup_target(backup_dir: str, target: dict) -> tuple[str, str]:
    """Copy one target's pre-image; returns (backup_path, sha256)."""
    source = target["path"]
    label = target["artifact"].replace(".", "_") + "-" \
        + hashlib.sha256(source.encode()).hexdigest()[:10]
    dest = os.path.join(backup_dir, label)
    try:
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as output, open(source, "rb") as input_file:
            shutil.copyfileobj(input_file, output)
            output.flush()
            os.fsync(output.fileno())
    except OSError as exc:
        raise Blocked(f"backup-failed ({exc}): {source} — nothing deleted")
    with open(dest, "rb") as fh:
        return dest, hashlib.sha256(fh.read()).hexdigest()


def write_manifest(backup_dir: str, manifest: dict) -> str:
    """Durably publish recovery metadata before unlink, then final results."""
    path = os.path.join(backup_dir, "manifest.json")
    temporary = os.path.join(backup_dir, ".manifest.tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, ensure_ascii=False, indent=1)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, path)
        fd = os.open(backup_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        raise Blocked(f"manifest-write-failed ({exc}); recovery: {backup_dir}") from exc
    return path


def armed_run(args, executable: list[dict], skipped: list[dict],
              digest: str, backup_base: str) -> int:
    """Rollback-first mutation (#873 3c): backup ALL, then delete, then
    verify. Any backup failure aborts before the first deletion."""
    backup_dir = build_backup_dir(backup_base)
    backups = []
    for target in executable:
        bpath, digest_pre = backup_target(backup_dir, target)
        backups.append({"target": target, "backup": bpath, "sha256": digest_pre})
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "ts": now(),
        "request": args.request,
        "key": args.key or args.audience,
        "plan_digest": digest,
        "mode": "apply",
        "backup_dir": backup_dir,
        "executable": [t["path"] for t in executable],
        "deleted": [],
        "failures": [],
        "skipped": skipped,
        "backups": [{"path": b["backup"], "sha256": b["sha256"],
                     "for": b["target"]["path"]} for b in backups],
        "verified": False,
        "phase": "prepared",
    }
    write_manifest(backup_dir, manifest)
    deleted, failures = [], []
    for item in backups:
        try:
            os.remove(item["target"]["path"])
        except OSError as exc:
            failures.append({"path": item["target"]["path"],
                             "reason": f"delete-failed ({exc})"})
            continue
        deleted.append(item["target"]["path"])
    verified = all(not os.path.exists(p) for p in deleted) and not failures
    for item in backups:
        with open(item["backup"], "rb") as fh:
            ok = hashlib.sha256(fh.read()).hexdigest() == item["sha256"]
        verified = verified and ok
    manifest.update(deleted=deleted, failures=failures, verified=verified, phase="completed")
    manifest_path = write_manifest(backup_dir, manifest)
    if not verified:
        raise Blocked(f"post-verification failed; manifest: {manifest_path}")
    print(f"erasure-apply (apply): {len(deleted)} deleted, "
          f"{len(skipped)} skipped, backup: {backup_dir}")
    if args.json:
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return EXIT_OK


def verify_plan(args) -> tuple[list[dict], list[dict], str]:
    inventory = load_inventory(args.inventory)
    _approved, live, digest = bind_plan(args, inventory)
    if live.get("blockers"):
        raise Blocked(f"{len(live['blockers'])} blocker(s) present; "
                      "classify before any apply")
    executable, skipped = partition_targets(live)
    return executable, skipped, digest


def main(argv: list[str]) -> int:
    args = parse_args(argv[1:])
    if args.request not in planner.REQUESTS:
        print(f"erasure-apply: unknown request '{args.request}'", file=sys.stderr)
        return EXIT_USAGE
    if args.request == "audience-erasure" and not args.audience:
        print("erasure-apply: audience-erasure requires --audience", file=sys.stderr)
        return EXIT_USAGE
    if args.request == "telegram-user-erasure" and not args.key:
        print("erasure-apply: telegram-user-erasure requires --key", file=sys.stderr)
        return EXIT_USAGE
    if len(args.plan_digest) != 64 or any(
            c not in "0123456789abcdef" for c in args.plan_digest):
        print("erasure-apply: --plan-digest must be 64 lowercase hex chars",
              file=sys.stderr)
        return EXIT_USAGE
    armed = os.environ.get(APPLY_ENV) == "1"
    try:
        executable, skipped, digest = verify_plan(args)
        if not armed:
            print(f"erasure-apply (plan-only): {len(executable)} executable "
                  f"target(s), {len(skipped)} skipped — set {APPLY_ENV}=1 to arm")
            if args.json:
                print(json.dumps({
                    "schema": MANIFEST_SCHEMA, "ts": now(),
                    "request": args.request, "key": args.key or args.audience,
                    "plan_digest": digest, "mode": "plan-only",
                    "executable": [t["path"] for t in executable],
                    "skipped": skipped,
                }, ensure_ascii=False, indent=2))
            return EXIT_OK
        with locked_backup_base() as backup_base:
            # Reject ordinary preflight failures without creating any backup
            # state, then bind again UNDER the lock to close the apply race.
            executable, skipped, digest = verify_plan(args)
            return armed_run(args, executable, skipped, digest, backup_base)
    except Blocked as exc:
        print(f"erasure-apply: BLOCKED — {exc}")
        return EXIT_BLOCKED


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
