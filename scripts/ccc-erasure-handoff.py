#!/usr/bin/env python3
"""ccc-erasure-handoff — erasure handoff manifest writer (#873 step 5).

Turns a planner plan into the ONE reviewable artifact a node hands to its
external owners during a closeout (design: issue #873 step-5 comment,
2026-09-04):

  $CCC_STATE_DIR/erasure-handoff-<request>-<stamp>.json  (+ a .md twin)

The node NEVER touches Family Wiki or operator material. The manifest is a
REQUEST, not an action: it lists what belongs to whom, what must be drained
first, the proposed Wiki disposition (annotate default), and an owner ACK
row that stays empty until the owner grants it (Fresh-approval manual ACK).

Sections:
  external                   planner external_handoff rows (verbatim owners)
  operator_decision_required owner=operator rows — decision only, no action
  wiki_disposition           per family-wiki artifact: annotate|archive|retain
                             proposal (annotate default), decision null
  drain_first                outbox-role classes + pending-entry counts —
                             reviewed/pruned BEFORE the apply step
  wiki_markers               (--queue) aggregated nunchi-p3-8 promotion
                             markers from a wiki-candidates queue: statuses
                             + fact ids, so promotion records travel with
                             the closeout (#1447 traceability)
  ack                        required=true, granted only by the owner
  next_apply                 the step-4 apply re-plans fresh and binds its
                             own digest; this manifest documents the
                             closeout START state (plan_digest)

Body-free by contract: ids, paths, counts, digests — never fact text or
secrets. Writes ONLY the two manifest files (0o600); everything else is
read-only. Exit codes: 0 ok, 2 usage, 4 blocked.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
# Single source for the canonical plan digest — the same handle the step-4
# apply boundary verifies against (hyphenated filenames, path-based import).
_apply_spec = importlib.util.spec_from_file_location(
    "ccc_erasure_apply", os.path.join(HERE, "ccc-erasure-apply.py"))
apply_mod = importlib.util.module_from_spec(_apply_spec)
_apply_spec.loader.exec_module(apply_mod)
_planner_spec = importlib.util.spec_from_file_location(
    "ccc_erasure_planner", os.path.join(HERE, "ccc-erasure-planner.py"))
planner = importlib.util.module_from_spec(_planner_spec)
_planner_spec.loader.exec_module(planner)

MANIFEST_SCHEMA = "ccc.erasure-handoff-manifest.v1"
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_BLOCKED = 4

WIKI_OWNERS = ("family-wiki",)
DEFAULT_DISPOSITION = "annotate"


class Blocked(Exception):
    """A fail-closed condition; the message is the body-free reason."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_args(argv: list[str]):
    parser = argparse.ArgumentParser(
        description="Erasure handoff manifest writer (#873 step 5) — writes "
                    "a reviewable closeout manifest; touches nothing else.")
    parser.add_argument("request", help="lifecycle request type")
    parser.add_argument("--audience", default=None)
    parser.add_argument("--key", default=None)
    parser.add_argument("--inventory", default=planner.DEFAULT_INVENTORY)
    parser.add_argument("--queue", default=None,
                        help="optional wiki-candidates queue file — aggregates "
                             "nunchi-p3-8 promotion markers for traceability")
    parser.add_argument("--out-dir", default=os.environ.get("CCC_STATE_DIR")
                        or os.path.expanduser("~/.claude/state"))
    parser.add_argument("--json", action="store_true",
                        help="print the manifest JSON to stdout as well")
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


def _validate_request(args) -> None:
    if args.request not in planner.REQUESTS:
        raise Blocked(f"unknown request '{args.request}'")
    if args.request == "audience-erasure" and not args.audience:
        raise Blocked("audience-erasure requires --audience")
    if args.request == "telegram-user-erasure" and not args.key:
        raise Blocked("telegram-user-erasure requires --key")


def _split_external(plan: dict) -> tuple[list[dict], list[dict], list[dict]]:
    """Split planner external_handoff rows into external verbatim, operator
    decision rows, and family-wiki disposition proposals (annotate default;
    the owner decides at ACK). A row is a Wiki-disposition candidate when its
    owner is family-wiki OR its handoff note references the Wiki — the queue
    FILE may be node-owned while the promoted material it produced lives in
    the Wiki (distill.wiki_candidates case)."""
    external, operator, wiki = [], [], []
    for row in plan.get("external_handoff", []):
        external.append({"artifact": row.get("artifact"),
                         "owner": row.get("owner"),
                         "note": row.get("reason", "")})
        note = row.get("reason", "")
        if row.get("owner") in WIKI_OWNERS or (
                row.get("owner") != "operator"
                and re.search(r"wiki", note, re.IGNORECASE)):
            wiki.append({"artifact": row.get("artifact"),
                         "proposal": DEFAULT_DISPOSITION, "decision": None})
        elif row.get("owner") == "operator":
            operator.append({"artifact": row.get("artifact"),
                             "note": "operator decision required"})
    return external, operator, wiki


def _collect_drain_first(scan: dict) -> list[dict]:
    """Outbox-role classes: reviewed/pruned BEFORE the apply step. Depths come
    from the planner scan — one diagnostic source, no copied counting rule."""
    return [{"artifact": row["artifact"], "path": row["path"],
             "present": row["present"], "pending": row["pending"]}
            for row in scan.get("outbox_depths", [])]


def _collect_wiki_markers(queue_path: str) -> dict:
    """Aggregate nunchi-p3-8 promotion markers from a wiki-candidates queue
    (#1447 traceability): statuses + the fact ids the batch queued."""
    markers, statuses = [], {"pending": 0, "merged": 0, "rejected": 0}
    try:
        with open(queue_path, encoding="utf-8") as fh:
            for line in fh:
                match = re.match(r"<!--\s*nunchi-p3-8\s+fact#(\d+)", line)
                if match:
                    markers.append(int(match.group(1)))
                stripped = line.strip()
                if stripped == "- status: pending":
                    statuses["pending"] += 1
                elif stripped == "- status: merged":
                    statuses["merged"] += 1
                elif stripped.startswith("- status: rejected"):
                    statuses["rejected"] += 1
    except OSError as exc:
        raise Blocked(f"cannot read --queue ({exc})")
    return {"queue_path": os.path.abspath(queue_path),
            "pending": statuses["pending"], "merged": statuses["merged"],
            "rejected": statuses["rejected"],
            "fact_ids": sorted(set(markers))}


def build_manifest(args, inventory: dict) -> dict:
    _validate_request(args)
    plan = planner.plan(
        args.request, inventory,
        args.audience if args.request == "audience-erasure" else None,
        args.key if args.request == "telegram-user-erasure" else None)
    digest = apply_mod.canonical_digest(plan)
    external, operator, wiki_disposition = _split_external(plan)
    scan = planner._run_scan(inventory)
    wiki_markers = _collect_wiki_markers(args.queue) if args.queue else None
    return {
        "schema": MANIFEST_SCHEMA,
        "ts": now(),
        "request": args.request,
        "key": args.key or args.audience,
        "plan_digest": digest,
        "external": external,
        "operator_decision_required": operator,
        "wiki_disposition": wiki_disposition,
        "drain_first": _collect_drain_first(scan),
        "wiki_markers": wiki_markers,
        "ack": {"required": True, "granted_at": None, "granted_by": None},
        "next_apply": {
            "hint": ("ccc-erasure-apply.py <request> --plan <fresh plan> "
                     "--plan-digest <digest> (with ERASURE_APPLY=1)"),
            "note": ("apply re-plans fresh and binds its own digest; this "
                     "manifest documents the closeout START state and its "
                     "ack must be granted before any apply"),
        },
    }


def render_md(manifest: dict) -> str:
    lines = [
        f"# erasure handoff manifest — {manifest['request']}"
        + (f" ({manifest['key']})" if manifest["key"] else ""),
        "",
        f"- generated: {manifest['ts']}",
        f"- plan digest: `{manifest['plan_digest']}`",
        "- owner ACK: **required, not granted**",
    ]
    if manifest["external"]:
        lines += ["", "## external handoff", ""]
        for row in manifest["external"]:
            lines.append(f"- {row['artifact']} → owner={row['owner']}"
                         + (f" — {row['note']}" if row.get("note") else ""))
    if manifest["operator_decision_required"]:
        lines += ["", "## operator decision required", ""]
        for row in manifest["operator_decision_required"]:
            lines.append(f"- {row['artifact']}")
    if manifest["wiki_disposition"]:
        lines += ["", "## wiki disposition (proposal — owner decides)", ""]
        for row in manifest["wiki_disposition"]:
            lines.append(f"- {row['artifact']}: propose **{row['proposal']}**, "
                         "decision: ___")
    lines += ["", "## drain first (outbox backlog)", ""]
    for row in manifest["drain_first"]:
        state = "present" if row["present"] else "absent"
        lines.append(f"- {row['artifact']}: {row['pending']} pending [{state}]")
    if manifest["wiki_markers"]:
        markers = manifest["wiki_markers"]
        lines += ["", "## promotion markers (#1447 batch)", ""]
        lines.append(f"- queue: `{markers['queue_path']}`")
        lines.append(f"- pending={markers['pending']} merged={markers['merged']} "
                     f"rejected={markers['rejected']}")
        lines.append(f"- fact ids: {markers['fact_ids'] or '(none)'}")
    lines += ["", "## next apply", "", f"- {manifest['next_apply']['hint']}",
              f"- {manifest['next_apply']['note']}"]
    return "\n".join(lines) + "\n"


def write_manifest(args, manifest: dict) -> tuple[str, str]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    os.makedirs(args.out_dir, exist_ok=True)
    # Two runs inside one second must not collide (O_EXCL): append a numeric
    # suffix — stays inside the inventory pattern's character class.
    for counter in range(0, 100):
        suffix = "" if counter == 0 else f"-{counter}"
        base = f"erasure-handoff-{args.request}-{stamp}{suffix}"
        json_path = os.path.join(args.out_dir, base + ".json")
        if not os.path.exists(json_path):
            break
    else:
        raise Blocked("manifest filename collision — out-dir saturated")
    paths = []
    for suffix2, payload in ((".json", json.dumps(manifest, ensure_ascii=False,
                                                  indent=1) + "\n"),
                             (".md", render_md(manifest))):
        path = os.path.join(args.out_dir, base + suffix2)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        paths.append(path)
    json_path, md_path = paths
    return json_path, md_path


def main(argv: list[str]) -> int:
    args = parse_args(argv[1:])
    try:
        inventory = load_inventory(args.inventory)
        manifest = build_manifest(args, inventory)
        json_path, md_path = write_manifest(args, manifest)
    except Blocked as exc:
        print(f"erasure-handoff: BLOCKED — {exc}")
        return EXIT_BLOCKED
    print("erasure-handoff: manifest written (owner ACK required):")
    print(f"  {json_path}")
    print(f"  {md_path}")
    if args.json:
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
