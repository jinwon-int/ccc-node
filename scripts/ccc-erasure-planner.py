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

Exit codes: 0 plan (blockers allowed, they are reported), 2 usage, 3 unknown
request type. Read-only is contractual — this script never writes, never
deletes, never creates a file.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

SCHEMA = "ccc.erasure-plan.v1"
INVENTORY_SCHEMA = "ccc.memory-artifact-inventory.v1"
DEFAULT_INVENTORY = str(Path(__file__).resolve().parent.parent
                        / "schemas" / "memory-artifact-inventory.v1.json")

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
REQUEST_SCOPES = {
    "audience-erasure": ("audience-scoped",),
    "node-decommission": ("node-local", "audience-scoped",
                          "upstream-adjacent", "external-adjacent"),
    "telegram-user-erasure": ("telegram",),
    "cache-rebuild": ("derived",),
    "prune-expired": ("outbox",),
}


def _expand(path: str) -> str:
    return os.path.expanduser(path)


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


def plan(request: str, inventory: dict, audience: str | None,
         key: str | None) -> dict:
    scopes = REQUEST_SCOPES.get(request, ())
    targets = []
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
    return {
        "schema": SCHEMA,
        "request": request,
        "key": key or audience,
        "read_only": True,
        "targets": targets,
        "external_handoff": external,
        "blockers": _unknown_blockers(inventory),
    }


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
    if request not in REQUESTS:
        print(f"erasure-planner: unknown request '{request}' "
              f"(known: {', '.join(REQUESTS)})", file=sys.stderr)
        return 3
    arg_name = REQUESTS[request]["arg"]
    value = None
    if arg_name:
        if arg_name not in args:
            print(f"erasure-planner: {request} requires {arg_name}", file=sys.stderr)
            return 2
        value = args[args.index(arg_name) + 1]
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
    doc = plan(request, inventory,
               value if request == "audience-erasure" else None,
               value if request == "telegram-user-erasure" else None)
    if "--json" in args:
        print(json.dumps(doc, ensure_ascii=False, indent=2))
    else:
        print(f"erasure plan — request={request} key={doc['key'] or '-'} "
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
