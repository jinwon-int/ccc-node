#!/usr/bin/env python3
"""ccc-erasure-planner — READ-ONLY cross-store erasure/decommission planner.

#873 step 2: the contract between every private/body-bearing artifact the
harness writes and the lifecycle requests that may touch them. This tool
MUTATES NOTHING: it resolves the live paths of each inventoried artifact
class through the same env chains the owning components use, computes the
exact targets and planned actions for one lifecycle request, and reports:

  targets          artifact id → resolved path, planned action, est. count/bytes
  external_handoff owners outside this node (family-wiki, operator) whose
                   material is NOT touched by any node-local run
  blockers         files under managed state roots that match NO inventory
                   entry (unknown artifacts) — a future apply slice must stop
                   on these until they are classified

Body-free by construction: output carries ids, paths, counts and byte
sizes — never file contents, never secret matches. The inventory lives in
schemas/memory-artifact-inventory.v1.json and is the machine-readable
policy registration the issue requires; --inventory overrides it for
staged edits and tests.

Requests (#873 §2):
  audience-erasure --audience NAME   wipe one audience-scoped state root
  node-decommission                  every class on this node
  telegram-user-erasure --key ID     redact one telegram user across surfaces
  cache-rebuild                      derived caches/indexes only
  prune-expired                      expired retry/rollback artifacts
  fact-correction                    pointer: handled by nunchi annotate/supersede
  scan --json                        inventory drift diagnostics (no request):
                                     unknown files in managed state roots +
                                     absent inventoried classes (#873 step 3)
  retention [--json]                 retention dry-run (no request, #1468):
                                     every file of a retention class (group a
                                     legacy stores, group b sensitive backups)
                                     with its mtime age, whether it is
                                     eligible for destruction now, and the
                                     date it becomes eligible

Retention classes (#1468, owner decision 2026-09-29): an inventory entry with
a "retention_policy" object is age-gated. Its files keep their "delete"
action only once they are older than max_age_days (default 30, measured from
the file's mtime); younger files are planned as "retain-until:<ISO date>",
and a file whose name matches a key-file pattern is planned as
"retain (key-file)" at ANY age. A path that a non-retention class resolves
as its live file is never a retention target. Eligibility is only a plan
state: destruction still happens exclusively through ccc-erasure-apply.py
(digest + blockers + owner-only + rollback-first, ERASURE_APPLY=1), which
needs a separate per-node owner approval.

Exit codes: 0 plan (blockers allowed, they are reported), 2 usage, 3 unknown
request type. Read-only is contractual — this script never writes, never
deletes, never creates a file.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "ccc.erasure-plan.v1"
INVENTORY_SCHEMA = "ccc.memory-artifact-inventory.v1"
DEFAULT_INVENTORY = str(Path(__file__).resolve().parent.parent
                        / "schemas" / "memory-artifact-inventory.v1.json")

RETENTION_SCHEMA = "ccc.erasure-retention.v1"
# Owner decision (#1468, 2026-09-29): groups a/b are kept 30 days, then
# become eligible for destruction at the apply boundary.
DEFAULT_RETENTION_DAYS = 30
# Operator knob: may only LENGTHEN retention. Shortening it below the
# inventory value is a policy change and needs a reviewed inventory edit.
RETENTION_DAYS_ENV = "CCC_ERASURE_RETENTION_DAYS"
# Upper clamp (100 years) so an absurd value cannot overflow date math.
MAX_RETENTION_DAYS = 36500
# Key files are always kept, at any age (#1468). Built-in, anchored regexes
# over the file NAME; the inventory's retention_defaults.key_file_patterns
# can only ADD to this list, never remove from it.
BUILTIN_KEY_FILE_PATTERNS = (
    r".*\.key(\..*)?",
    r".*\.pem(\..*)?",
    r".*\.(p12|pfx|jks|keystore)(\..*)?",
    r"id_(rsa|dsa|ecdsa|ed25519)(_sk)?(\..*)?",
    r"(.*[._-])?credentials?([._-].*)?",
)

REQUESTS = {
    "audience-erasure": {"arg": "--audience"},
    "node-decommission": {"arg": None},
    "telegram-user-erasure": {"arg": "--key"},
    "cache-rebuild": {"arg": None},
    "prune-expired": {"arg": None},
    "fact-correction": {"arg": None},
}

# Path-class prefixes a request scope covers. path_class may carry extra
# qualifiers ("node-local audit append-only"), so this is prefix matching.
# Only ownership-scoped requests need this additional restriction. For
# cache/prune/user requests the explicit inventory action is authoritative:
# roles such as "derived" and "outbox" are NOT path-class prefixes.
REQUEST_SCOPES = {
    "audience-erasure": ("audience-scoped",),
    "node-decommission": ("node-local", "audience-scoped",
                          "upstream-adjacent", "external-adjacent"),
}


def _expand(path: str) -> str:
    return os.path.expanduser(path)


def _spec_base_and_literal(cand: dict) -> tuple[str | None, str | None]:
    """(base_dir, literal_path) for one resolve candidate spec.

    join form: env value is the base directory, cand["path"] the suffix.
    absolute form: cand["path"] is the literal path itself.
    """
    env_name = cand.get("env")
    base = os.environ.get(env_name) if env_name else None
    if cand.get("join"):
        if not base:
            return None, None
        return _expand(base), _expand(base.rstrip("/") + "/" + cand["path"].lstrip("/"))
    path = _expand(cand["path"])
    return os.path.dirname(path) or "/", path


def _pattern_specs(entry: dict) -> list[tuple[str, re.Pattern[str]]]:
    """Anchored (base_dir, regex) pairs from kind=pattern resolve candidates.

    Pattern candidates let one class own DATED artifacts (bench-YYYYMMDD.md)
    without inventing files: the regex is matched against sibling FILE NAMES
    under the candidate's base directory — name matching only, no glob
    expansion, no filesystem writes. join form: env is the base, path is the
    regex; absolute form: dirname is the base, basename the regex.
    """
    specs = []
    for cand in (entry.get("resolve") or {}).get("candidates", []):
        if cand.get("kind") != "pattern":
            continue
        env_name = cand.get("env")
        base_env = os.environ.get(env_name) if env_name else None
        if cand.get("join"):
            if not base_env:
                continue
            base = _expand(base_env)
            regex_src = cand["path"]
        else:
            path = _expand(cand["path"])
            base = os.path.dirname(path) or "/"
            regex_src = os.path.basename(path)
        try:
            specs.append((base, re.compile(regex_src)))
        except re.error:
            continue  # a broken pattern classifies nothing — never aborts
    return specs


def secondary_paths(entry: dict) -> list[str]:
    """Every existing file that belongs to this class BEYOND the primary
    resolution: extra_paths literals + pattern matches.

    extra_paths are additional literal candidates with the same shape as
    resolve.candidates — multi-file classes (the managed cron logs, the
    runtime locks) register every file they write. Returns deduplicated
    absolute paths; the primary resolve_entry path is NOT included.
    """
    primary = resolve_entry(entry)
    primary_path = os.path.abspath(primary) if primary else None
    out: list[str] = []
    for spec in entry.get("extra_paths", []):
        _base, literal = _spec_base_and_literal(spec)
        if literal and os.path.isfile(literal):
            p = os.path.abspath(literal)
            if p != primary_path and p not in out:
                out.append(p)
    for base, regex in _pattern_specs(entry):
        if not os.path.isdir(base):
            continue
        try:
            names = sorted(os.listdir(base))
        except OSError:
            continue
        for name in names:
            if not regex.fullmatch(name):
                continue
            p = os.path.abspath(os.path.join(base, name))
            if not os.path.isfile(p):
                continue
            if p == primary_path:
                continue
            if p not in out:
                out.append(p)
    return out


def resolve_entry(entry: dict) -> str | None:
    """Resolve one artifact entry's live path through its env chain.

    Kind-strict on purpose: a file-class candidate whose default path happens
    to be an existing DIRECTORY must never resolve to that directory — the
    unknown-artifact sweep treats resolved directories as classified subtrees,
    so a poisoned resolution would hide every unknown file beneath it.
    Returns the last candidate path when nothing exists (absent reporting).
    """
    for cand in (entry.get("resolve") or {}).get("candidates", []):
        env_name = cand.get("env")
        base = os.environ.get(env_name) if env_name else None
        if cand.get("join"):
            # env is a BASE directory; the candidate path is a suffix under it.
            if not base:
                continue  # env unset → this candidate cannot resolve
            path = _expand(base.rstrip("/") + "/" + cand["path"].lstrip("/"))
        elif base:
            path = _expand(base)
        else:
            path = _expand(cand["path"])
        if cand.get("kind") == "dir":
            if os.path.isdir(path):
                return path
        elif os.path.isfile(path):
            return path
    cands = (entry.get("resolve") or {}).get("candidates") or []
    return _expand(cands[-1]["path"]) if cands else None


def _iso(epoch: float) -> str:
    try:
        stamp = datetime.fromtimestamp(int(epoch), timezone.utc)
    except (OverflowError, OSError, ValueError):
        return "9999-12-31T23:59:59Z"   # beyond any real clock: never eligible
    return stamp.isoformat(timespec="seconds").replace("+00:00", "Z")


def _positive_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return min(number, MAX_RETENTION_DAYS) if number >= 1 else None


def retention_days(inventory: dict, entry: dict) -> int:
    """Effective retention (days) for one retention class.

    Precedence: entry retention_policy.max_age_days → inventory
    retention_defaults.max_age_days → DEFAULT_RETENTION_DAYS. Invalid or
    non-positive values fall through (a broken value never means "delete
    now"). The env knob is honoured only when it LENGTHENS retention.
    """
    policy = entry.get("retention_policy") or {}
    defaults = inventory.get("retention_defaults") or {}
    days = (_positive_int(policy.get("max_age_days"))
            or _positive_int(defaults.get("max_age_days"))
            or DEFAULT_RETENTION_DAYS)
    override = _positive_int(os.environ.get(RETENTION_DAYS_ENV))
    if override and override > days:
        days = override
    return days


def _key_file_patterns(inventory: dict) -> list[re.Pattern[str]]:
    sources = list(BUILTIN_KEY_FILE_PATTERNS)
    extra = (inventory.get("retention_defaults") or {}).get("key_file_patterns")
    if isinstance(extra, list):
        sources.extend(str(x) for x in extra)
    out = []
    for src in sources:
        try:
            out.append(re.compile(src))
        except re.error:
            continue
    return out


def is_key_file(inventory: dict, path: str) -> bool:
    name = os.path.basename(path)
    return any(rx.fullmatch(name) for rx in _key_file_patterns(inventory))


def _live_claims(inventory: dict) -> tuple[set[str], list[str]]:
    """Files/dirs owned by NON-retention classes (the live resolver view).

    A retention pattern may name a path that is still a live store under the
    current env (e.g. ~/.nunchi/facts.db when NUNCHI_DB is unset) — the live
    class wins and the path is never a retention target.
    """
    files: set[str] = set()
    dirs: list[str] = []
    for entry in inventory.get("artifacts", []):
        if entry.get("retention_policy"):
            continue
        resolved = resolve_entry(entry)
        if resolved:
            resolved = os.path.abspath(resolved)
            is_dir = any(c.get("kind") == "dir" for c in
                         (entry.get("resolve") or {}).get("candidates", []))
            if is_dir and os.path.isdir(resolved):
                dirs.append(resolved)
            elif not is_dir and os.path.isfile(resolved):
                files.add(resolved)
        files.update(secondary_paths(entry))
    return files, dirs


def retention_paths(inventory: dict, entry: dict,
                    claims: tuple[set[str], list[str]] | None = None) -> list[str]:
    """Secondary paths of a retention class minus live-claimed ones."""
    files, dirs = claims if claims is not None else _live_claims(inventory)
    out = []
    for path in secondary_paths(entry):
        if path in files or any(path.startswith(d + os.sep) for d in dirs):
            continue
        out.append(path)
    return out


def retention_verdict(inventory: dict, entry: dict, path: str,
                      now: float | None = None) -> dict:
    """Age verdict for one file of a retention class. Body-free: name/mtime
    only — the file is never opened."""
    policy = entry.get("retention_policy") or {}
    days = retention_days(inventory, entry)
    verdict = {"group": policy.get("group"), "max_age_days": days,
               "age_source": "mtime", "mtime": None, "eligible_at": None,
               "eligible": False, "reason": "unreadable"}
    try:
        meta = os.lstat(path)
    except OSError:
        return verdict
    current = time.time() if now is None else now
    eligible_at = int(meta.st_mtime) + days * 86400
    verdict["mtime"] = _iso(meta.st_mtime)
    if is_key_file(inventory, path):
        verdict["reason"] = "key-file"          # always kept, any age
        return verdict
    verdict["eligible_at"] = _iso(eligible_at)
    if os.path.islink(path):
        verdict["reason"] = "symlink"           # never a destruction target
        return verdict
    verdict["eligible"] = current >= eligible_at
    verdict["reason"] = "retention-expired" if verdict["eligible"] \
        else "within-retention"
    return verdict


def gated_action(action: str, verdict: dict) -> str:
    """Destruction actions stay only for eligible files; everything else in
    a retention class is planned as a retain variant (apply skips it)."""
    if not action.startswith("delete") or verdict.get("eligible"):
        return action
    if verdict.get("reason") in ("key-file", "symlink", "unreadable"):
        return f"retain ({verdict['reason']})"
    return f"retain-until:{verdict['eligible_at']}"


def _scan(inventory: dict) -> tuple[set[str], set[str], list[str]]:
    """Strict resolution sweep → (known_files, known_dirs, sweep_roots).

    known_dirs are classified subtrees: unknown detection skips anything
    beneath them, because their contents are owned by the inventoried class.
    """
    known_files: set[str] = set()
    known_dirs: list[str] = []
    roots: list[str] = []
    for entry in inventory.get("artifacts", []):
        entry_is_dir = any(c.get("kind") == "dir"
                           for c in (entry.get("resolve") or {}).get("candidates", []))
        resolved = resolve_entry(entry)
        if not resolved:
            continue
        resolved = os.path.abspath(resolved)
        # Kind-aware: a file-class entry whose default path resolves to a
        # directory (the common case for an absent file) contributes NOTHING —
        # classifying that directory would hide unknown artifacts under it.
        if entry_is_dir and os.path.isdir(resolved):
            if resolved not in known_dirs:
                known_dirs.append(resolved)
                roots.append(resolved)
            for suffix in entry.get("related_dirs", []):
                related = os.path.join(resolved, suffix)
                if os.path.isdir(related) and related not in known_dirs:
                    known_dirs.append(related)
                    roots.append(related)
        elif not entry_is_dir and os.path.isfile(resolved):
            known_files.add(resolved)
            parent = os.path.dirname(resolved)
            if parent not in roots:
                roots.append(parent)
        for secondary in secondary_paths(entry):
            known_files.add(secondary)
            parent = os.path.dirname(secondary)
            if parent not in roots:
                roots.append(parent)
    return known_files, known_dirs, roots


def _unknown_blockers(inventory: dict, max_blockers: int = 20) -> list[dict]:
    """Unclassified files inside managed state roots (#873 blockers).

    One directory level only — subdirectories of managed roots are their own
    inventoried classes' business. Capped so a chaotic state dir cannot turn
    the plan into a wall of names.
    """
    known_files, known_dirs, roots = _scan(inventory)
    blockers: list[dict] = []
    truncated = False
    for root in roots:
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            path = os.path.abspath(os.path.join(root, name))
            if path in known_files or path in known_dirs:
                continue
            if any(path.startswith(kd + os.sep) for kd in known_dirs):
                continue
            if os.path.isdir(path):
                continue
            if len(blockers) >= max_blockers:
                truncated = True
                break
            blockers.append({"path": path, "reason": "not-in-inventory"})
        if truncated:
            break
    if truncated:
        blockers.append({"path": "(…more)", "reason": "blocker-list-truncated"})
    return blockers


def _estimate_entry(entry: dict, resolved: str | None) -> dict:
    """Estimate over the resolved path PLUS related_dirs (same artifact class)."""
    total = _estimate(resolved)
    if resolved and os.path.isdir(resolved):
        for suffix in entry.get("related_dirs", []):
            more = _estimate(os.path.join(resolved, suffix))
            total["files"] += more["files"]
            total["bytes"] += more["bytes"]
    return total


def _estimate(path: str | None) -> dict:
    if not path or not os.path.exists(path):
        return {"files": 0, "bytes": 0}
    if os.path.isdir(path):
        files = bytes_ = 0
        for root, _dirs, names in os.walk(path):
            for name in names:
                try:
                    bytes_ += os.path.getsize(os.path.join(root, name))
                    files += 1
                except OSError:
                    pass
        return {"files": files, "bytes": bytes_}
    try:
        return {"files": 1, "bytes": os.path.getsize(path)}
    except OSError:
        return {"files": 0, "bytes": 0}


def _secondary_targets(inventory: dict, entry: dict, action: str,
                       cache: dict, now: float) -> list[dict]:
    """Per-path targets beyond the primary resolution. Retention classes
    (#1468) get a per-file age gate: live-claimed paths are excluded, key
    files are always kept, and only expired files keep a delete action.
    ``cache`` holds the live-claim sweep so one plan computes it once."""
    out = []
    if entry.get("retention_policy"):
        if "claims" not in cache:
            cache["claims"] = _live_claims(inventory)
        for path in retention_paths(inventory, entry, cache["claims"]):
            verdict = retention_verdict(inventory, entry, path, now)
            out.append({"artifact": entry["id"], "path": path, "present": True,
                        "action": gated_action(action, verdict),
                        "estimate": _estimate(path), "retention": verdict})
        return out
    for path in secondary_paths(entry):
        out.append({"artifact": entry["id"], "path": path, "present": True,
                    "action": action, "estimate": _estimate(path)})
    return out


def plan(request: str, inventory: dict, audience: str | None,
         key: str | None) -> dict:
    scopes = REQUEST_SCOPES.get(request, ())
    targets = []
    cache: dict = {}
    now = time.time()
    external = []
    for entry in inventory.get("artifacts", []):
        req_actions = entry.get("requests") or {}
        if request not in req_actions:
            continue
        action = req_actions[request]
        if action == "external-handoff":
            external.append({"artifact": entry["id"],
                             "owner": entry.get("owner", "external"),
                             "reason": entry.get("handoff_note", "")})
            continue
        if action.startswith("out-of-scope"):
            continue
        path_class = str(entry.get("path_class", ""))
        if scopes and not path_class.startswith(tuple(scopes)):
            continue
        resolved = resolve_entry(entry)
        if (request == "audience-erasure" and audience
                and entry.get("audience_scoped_subpath") and resolved):
            resolved = os.path.join(resolved, audience)
        present = bool(resolved and (os.path.isdir(resolved)
                                     or os.path.isfile(resolved)))
        targets.append({
            "artifact": entry["id"],
            "path": resolved,
            "present": present,
            "action": action,
            "estimate": _estimate_entry(entry, resolved),
        })
        # Multi-file classes (extra_paths + pattern matches) target per path:
        # an apply slice must name every file it would touch, never a class.
        targets.extend(_secondary_targets(inventory, entry, action, cache, now))
    return {
        "schema": SCHEMA,
        "request": request,
        "key": key or audience,
        "read_only": True,
        "targets": targets,
        "external_handoff": external,
        "blockers": _unknown_blockers(inventory),
    }


def _outbox_depths(inventory: dict) -> list[dict]:
    """Pending-entry estimates for outbox-role classes (#873 step 5).

    Drain-first facts for the closeout checklist: an outbox with a backlog
    must be reviewed/pruned before any decommission apply. Counts only —
    dir entries or non-empty lines, never contents.
    """
    depths = []
    for entry in inventory.get("artifacts", []):
        if entry.get("role") != "outbox":
            continue
        resolved = resolve_entry(entry)
        present = bool(resolved and (os.path.isfile(resolved)
                                     or os.path.isdir(resolved)))
        pending = 0
        if present and os.path.isdir(resolved):
            for root, _dirs, names in os.walk(resolved):
                pending += len(names)
        elif present:
            try:
                with open(resolved, encoding="utf-8", errors="replace") as fh:
                    pending = sum(1 for line in fh if line.strip())
            except OSError:
                pending = 0
        depths.append({"artifact": entry["id"], "path": resolved,
                       "present": present, "pending": pending})
    return depths


def _run_scan(inventory: dict) -> dict:
    """#873 step 3 — inventory drift/unknown diagnostics for the security
    audit and doctor. Versioned, body-free, read-only."""
    known_files, known_dirs, roots = _scan(inventory)
    unknown = _unknown_blockers(inventory)
    absent = []
    for entry in inventory.get("artifacts", []):
        resolved = resolve_entry(entry)
        if not resolved or not (os.path.isdir(resolved)
                                or os.path.isfile(resolved)):
            absent.append({"artifact": entry["id"], "path": resolved})
    return {
        "schema": "ccc.memory-inventory-scan.v1",
        "read_only": True,
        "roots": roots,
        "known_dirs": sorted(known_dirs),
        "known_files_count": len(known_files),
        "unknown": unknown,
        "absent": absent,
        "outbox_depths": _outbox_depths(inventory),
    }


def retention_report(inventory: dict) -> dict:
    """#1468 dry-run: every file of every retention class with its verdict.
    Read-only, body-free (paths, dates, counts)."""
    now = time.time()
    claims = _live_claims(inventory)
    entries = []
    for entry in inventory.get("artifacts", []):
        if not entry.get("retention_policy"):
            continue
        action = (entry.get("requests") or {}).get("prune-expired", "retain")
        for path in retention_paths(inventory, entry, claims):
            verdict = retention_verdict(inventory, entry, path, now)
            planned = gated_action(action, verdict)
            if verdict["eligible"] and not planned.startswith("delete"):
                # Age passed, but the class itself never destroys (retain).
                verdict.update(eligible=False, reason="class-retain")
            entries.append({"artifact": entry["id"], "path": path, **verdict,
                            "planned_action": planned})
    eligible = sum(1 for e in entries if e["eligible"])
    keys = sum(1 for e in entries if e["reason"] == "key-file")
    return {
        "schema": RETENTION_SCHEMA,
        "read_only": True,
        "generated_at": _iso(now),
        "default_max_age_days": DEFAULT_RETENTION_DAYS,
        "entries": entries,
        "summary": {"files": len(entries), "eligible": eligible,
                    "retained": len(entries) - eligible, "key_files": keys},
        "apply_note": ("eligible means planned, not deleted: destruction runs "
                       "only via a prune-expired plan through "
                       "ccc-erasure-apply.py with ERASURE_APPLY=1 and a "
                       "separate per-node owner approval"),
    }


def _print_retention_human(doc: dict) -> None:
    summary = doc["summary"]
    print(f"erasure retention — {summary['files']} file(s): "
          f"{summary['eligible']} eligible now, {summary['retained']} retained "
          f"({summary['key_files']} key file(s)) (READ-ONLY, nothing deleted)")
    for e in doc["entries"]:
        group = e.get("group") or "-"
        if e["eligible"]:
            state = f"eligible since {e['eligible_at']}"
        elif e["reason"] == "within-retention":
            state = f"retained until {e['eligible_at']}"
        else:
            state = f"retained ({e['reason']})"
        print(f"  - [{group}] {e['artifact']}: {state}")
        print(f"      {e['path']}")
    print(f"  note: {doc['apply_note']}")


def _print_plan_human(doc: dict) -> None:
    print(f"erasure plan — request={doc['request']} key={doc['key'] or '-'} "
          f"(READ-ONLY, no mutation performed)")
    for t in doc["targets"]:
        est = t["estimate"]
        state = "present" if t["present"] else "absent"
        print(f"  - {t['artifact']}: {t['action']} [{state}] "
              f"files={est['files']} bytes={est['bytes']}")
        if t["path"]:
            print(f"      {t['path']}")
    for h in doc["external_handoff"]:
        print(f"  - external handoff: {h['artifact']} (owner={h['owner']})")
    for b in doc["blockers"]:
        print(f"  ! blocker: {b['path']} — {b['reason']}")
    if doc["blockers"]:
        print(f"  {len(doc['blockers'])} blocker(s): an apply slice must stop "
              "until these are classified.")


def _run_diagnostic(request: str, inventory: dict, as_json: bool) -> int:
    """Request-free read-only reports: scan (always JSON) and retention."""
    if request == "scan":
        print(json.dumps(_run_scan(inventory), ensure_ascii=False, indent=2))
        return 0
    doc = retention_report(inventory)
    if as_json:
        print(json.dumps(doc, ensure_ascii=False, indent=2))
    else:
        _print_retention_human(doc)
    return 0


def main(argv: list[str]) -> int:
    args = argv[1:]
    if "--help" in args or "-h" in args or not args:
        print(__doc__)
        return 0
    inventory_path = DEFAULT_INVENTORY
    while "--inventory" in args:
        idx = args.index("--inventory")
        if idx + 1 >= len(args):
            print("erasure-planner: --inventory requires a value", file=sys.stderr)
            return 2
        inventory_path = args[idx + 1]
        args = args[:idx] + args[idx + 2:]
    request = args[0] if args else ""
    try:
        with open(inventory_path, encoding="utf-8") as fh:
            inventory = json.load(fh)
    except (OSError, ValueError) as exc:
        print(f"erasure-planner: cannot load inventory {inventory_path}: {exc}",
              file=sys.stderr)
        return 2
    if inventory.get("schema") != INVENTORY_SCHEMA:
        print(f"erasure-planner: inventory schema mismatch: "
              f"{inventory.get('schema')}", file=sys.stderr)
        return 2
    if request in ("scan", "retention"):
        return _run_diagnostic(request, inventory, "--json" in args)
    if request not in REQUESTS:
        print(f"erasure-planner: unknown request '{request}' "
              f"(known: {', '.join(REQUESTS)}, scan, retention)", file=sys.stderr)
        return 3
    arg_name = REQUESTS[request]["arg"]
    value = None
    if arg_name:
        if arg_name not in args:
            print(f"erasure-planner: {request} requires {arg_name}", file=sys.stderr)
            return 2
        value = args[args.index(arg_name) + 1]
    doc = plan(request, inventory,
               value if request == "audience-erasure" else None,
               value if request == "telegram-user-erasure" else None)
    if "--json" in args:
        print(json.dumps(doc, ensure_ascii=False, indent=2))
    else:
        _print_plan_human(doc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
