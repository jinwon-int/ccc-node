#!/usr/bin/env python3
"""Standard rescreen rotation generator for the skills-intake review lane (#2028).

Replaces the ad-hoc /root/rescreen-*.py family: instead of hand-picked
reviewer lists, it builds the reviewer pool from live broker state, skips
nodes with recent failures (recording why), spreads assignments across
review providers deterministically, and records the reason every node was or
was not chosen.

Usage:
  python3 rescreen-rotation.py --cases CASES.json [--names a,b,...]
        [--dry-run] [--output OUT.json] [--exclude-hours N] [--node-agents FILE]

Input CASES.json: {"<skill-name>": {node, provider, branch, pr, skill_sha256,
tree_sha256, files:[{path, content_b64, executable?}]}} — the same dispatch
case files the ad-hoc rescreen scripts consumed.

Reviewer pool (per broker): keyring workers ∩ online workers − author node.
Primary broker first, then each CCC_SKILL_PROMOTION_REMOTE_BROKERS entry
(#2024). A reviewer with a task.failed audit event inside
--exclude-hours (default 6) is skipped with the failure count as the reason.
Assignments rotate through provider groups (implementationCapability from the
broker; nodes without it report provider "unknown") starting at the case
ordinal, so identical state produces identical distribution.

RESCREEN_DRYRUN=1 (or --dry-run) builds and reports the rotation without
dispatching anything.
"""
from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import os
import pathlib
import sys
from datetime import datetime, timedelta, timezone

RECENT_FAILURE_LIMIT = 1  # >=1 task.failed inside the window excludes the node


def _load_promoter() -> tuple[object, pathlib.Path]:
    """Load the canonical promoter module for its broker/dispatch internals."""
    candidates = [
        pathlib.Path(os.environ.get("CCC_SKILL_PROMOTION_HOOK", "")),
        pathlib.Path(__file__).resolve().parent / "ccc-skill-promotion.py",
        pathlib.Path.home() / ".claude" / "hooks" / "ccc-skill-promotion.py",
    ]
    for candidate in candidates:
        if candidate.is_file():
            spec = importlib.util.spec_from_file_location("csp_rescreen", candidate)
            module = importlib.util.module_from_spec(spec)
            sys.modules["csp_rescreen"] = module
            spec.loader.exec_module(module)
            return module, candidate
    raise SystemExit("rescreen-rotation: promoter module (ccc-skill-promotion.py) not found")


def _local_workers_full(m, config, secret: str) -> dict[str, dict]:
    completed = m._run(["curl", "-fsS", "-H", f"x-a2a-edge-secret: {secret}",
                        f"{config.broker_url}/workers?include=stale_read_path&limit=100"])
    payload = json.loads(completed.stdout.decode("utf-8"))
    return {row["nodeId"]: row for row in payload.get("items", []) if isinstance(row, dict)}


def _remote_workers_full(m, config, rb) -> dict[str, dict]:
    script = (
        'S=$(' + rb["secret_cmd"] + ') || exit 75\n'
        'curl -fsS -H "x-a2a-edge-secret: $S" "' + rb["broker_url"]
        + '/workers?include=stale_read_path&limit=100"\n'
    )
    payload = json.loads(m._remote_ssh_capture(config, rb, script, timeout=120).decode("utf-8"))
    return {row["nodeId"]: row for row in payload.get("items", []) if isinstance(row, dict)}


def _recent_failures(m, config, rb, secret: str, window_hours: int) -> dict[str, int]:
    """task.failed audit counts per actor inside the exclusion window."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    if rb is None:
        completed = m._run(["curl", "-fsS", "-H", f"x-a2a-edge-secret: {secret}",
                            f"{config.broker_url}/audit?action=task.failed&limit=200"])
        raw = completed.stdout.decode("utf-8")
    else:
        script = (
            'S=$(' + rb["secret_cmd"] + ') || exit 75\n'
            'curl -fsS -H "x-a2a-edge-secret: $S" "' + rb["broker_url"]
            + '/audit?action=task.failed&limit=200"\n'
        )
        raw = m._remote_ssh_capture(config, rb, script, timeout=120).decode("utf-8")
    payload = json.loads(raw)
    counts: dict[str, int] = {}
    for event in payload.get("items", []):
        actor = event.get("actorId") if isinstance(event, dict) else None
        created = event.get("createdAt") if isinstance(event, dict) else None
        if not isinstance(actor, str) or not isinstance(created, str):
            continue
        try:
            when = datetime.fromisoformat(created.replace("Z", "+00:00"))
        except ValueError:
            continue
        if when >= cutoff:
            counts[actor] = counts.get(actor, 0) + 1
    return counts


def _provider_of(worker_row: dict) -> tuple[str, str]:
    cap = worker_row.get("implementationCapability") if isinstance(worker_row, dict) else None
    if isinstance(cap, dict):
        provider = str(cap.get("providerId") or "unknown")
        tier = str(cap.get("modelTier") or "unknown")
        return provider, tier
    return "unknown", "unknown"


def _build_pool(m, config, secret: str, keyring: set[str], exclude_hours: int,
                node_agents: dict[str, dict]) -> tuple[list[dict], list[dict]]:
    """Deterministic, provider-diverse reviewer pool across all brokers.

    Returns (pool, exclusions): pool entries are
    {node, broker, broker_name, provider, model_tier} in rotation order —
    provider groups (sorted, "unknown" last) round-robin, nodes sorted inside
    each group. Exclusions list nodes that are keyring+online but were dropped
    for recent failures, with the reason."""
    brokers: list[tuple[str, object]] = [("primary", None)]
    brokers += [(rb["name"], rb) for rb in config.remote_brokers]

    exclusions: list[dict] = []
    pool: list[dict] = []
    for broker_name, rb in brokers:
        online = (m._broker_online_worker_ids(config, secret) if rb is None
                  else m._remote_online_worker_ids(config, rb))
        workers_full = (_local_workers_full(m, config, secret) if rb is None
                        else _remote_workers_full(m, config, rb))
        failures = _recent_failures(m, config, rb, secret, exclude_hours)
        eligible = sorted(keyring & online)
        for node in eligible:
            worker_row = workers_full.get(node, {})
            provider, tier = node_agents.get(node, {}).get("provider"), node_agents.get(node, {}).get("model")
            if not provider:
                provider, tier = _provider_of(worker_row)
            recent = failures.get(node, 0)
            if recent >= RECENT_FAILURE_LIMIT:
                exclusions.append({
                    "node": node, "broker": broker_name,
                    "reason": f"{recent} task.failed event(s) in the last {exclude_hours}h",
                })
                continue
            pool.append({
                "node": node, "broker": broker_name, "rb": rb,
                "provider": provider, "model_tier": tier,
            })

    # Provider-diverse rotation: cycle provider groups in sorted order
    # ("unknown" last), nodes sorted inside each group — deterministic.
    groups: dict[str, list[dict]] = {}
    for entry in pool:
        groups.setdefault(entry["provider"], []).append(entry)
    ordered_groups = [g for g in sorted(groups) if g != "unknown"] + (
        ["unknown"] if "unknown" in groups else [])
    rotation: list[dict] = []
    depth = 0
    while any(groups[g] for g in ordered_groups):
        progressed = False
        for g in ordered_groups:
            if groups[g]:
                rotation.append(groups[g].pop(0))
                depth += 1
                progressed = True
        if not progressed:
            break
    del depth
    return rotation, exclusions


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", required=True)
    parser.add_argument("--names", default="", help="comma-separated skill-name filter")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output", default="")
    parser.add_argument("--exclude-hours", type=int, default=6)
    parser.add_argument("--node-agents", default="", help="JSON {node:{provider,model}} override")
    args = parser.parse_args()

    if os.environ.get("RESCREEN_DRYRUN") == "1":
        args.dry_run = True

    m, _ = _load_promoter()
    cfg = m._config(dict(os.environ))
    secret = os.environ["A2A_EDGE_SECRET"]
    keyring = set(m._keyring_worker_ids(cfg))

    node_agents = {}
    if args.node_agents and pathlib.Path(args.node_agents).is_file():
        node_agents = json.loads(pathlib.Path(args.node_agents).read_text(encoding="utf-8"))

    cases = json.loads(pathlib.Path(args.cases).read_text(encoding="utf-8"))
    if isinstance(cases, list):  # ad-hoc rescreen-*.py format: index by name
        cases = {c["name"]: c for c in cases if isinstance(c, dict) and "name" in c}
    wanted = [n.strip() for n in args.names.split(",") if n.strip()]
    names = wanted or sorted(cases)

    rotation, exclusions = _build_pool(m, cfg, secret, keyring, args.exclude_hours, node_agents)
    if not rotation:
        print(json.dumps({"ok": False, "error": "no eligible reviewer online"}, ensure_ascii=False))
        return 1

    results = []
    for index, name in enumerate(names):
        case = cases.get(name)
        if not case:
            results.append({"name": name, "outcome": "rescreen-skipped", "reason": "no-case"})
            continue
        author = case.get("node")
        # Deterministic rotation: start at the case ordinal, walk forward, and
        # take the first pool entry that is not the author. Skipped nodes are
        # recorded with the reason so the assignment is auditable.
        start = index % len(rotation)
        chosen = None
        skipped: list[dict] = []
        for offset in range(len(rotation)):
            entry = rotation[(start + offset) % len(rotation)]
            if entry["node"] == author:
                skipped.append({"node": entry["node"], "reason": "author node excluded"})
                continue
            chosen = entry
            break
        if chosen is None:
            results.append({"name": name, "outcome": "rescreen-skipped",
                            "reason": "no eligible reviewer (author-exclusion)",
                            "skipped": skipped})
            continue

        entry_result = {
            "name": name,
            "reviewer": chosen["node"],
            "broker": chosen["broker"],
            "review_provider": chosen["provider"],
            "review_model_tier": chosen["model_tier"],
            "reason": (
                f"rotation[{(start + offset) % len(rotation)}] of {len(rotation)}: "
                f"keyring+online on {chosen['broker']}, provider {chosen['provider']} "
                f"(diversity interleave, deterministic by case order)"
            ),
            "skipped": skipped,
        }

        head = m._branch_head_sha(cfg, case["branch"])
        procedure = m._worker_procedure_from_docs(
            cfg.a2a_nexus_dir, doc_name="skills-intake-review.md", end_marker=m._DISPATCH_DOC_END)
        files = tuple(
            m.SnapshotFile(relative=f["path"], content=base64.b64decode(f["content_b64"]),
                           executable=bool(f.get("executable", False)))
            for f in case["files"])
        cand = m.Candidate(node=case["node"], provider=case["provider"], name=name,
                           skill_sha256=case.get("skill_sha256", tree_of(case)),
                           tree_sha256=case["tree_sha256"],
                           source_dir=pathlib.Path("/tmp"), files=files, description="")
        now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"{index:02d}"
        manifest = m._build_dispatch_manifest(
            cfg, cand, pr_number=str(case["pr"]), branch=case["branch"], head=head,
            reviewer=chosen["node"],
            broker_id=(m._remote_broker_id(cfg, chosen["rb"]) if chosen["rb"] is not None
                       else m._broker_id(cfg, secret)),
            broker_url=(chosen["rb"]["broker_url"] if chosen["rb"] is not None else cfg.broker_url),
            procedure=procedure, inventory=[], now=now)
        manifest["lanes"][0]["payload"]["scope"] = "public-elevation"

        if args.dry_run:
            entry_result["dryrun"] = True
            results.append(entry_result)
            continue

        if chosen["rb"] is not None:
            try:
                result = m._remote_dispatch_round(cfg, chosen["rb"], manifest)
                trow = (result.get("results") or [{}])[0]
                entry_result["task"] = trow.get("taskId")
            except m.PromotionError as error:
                entry_result["outcome"] = "rescreen-dispatch-failed"
                entry_result["error"] = str(error)[:120]
        else:
            manifest_path = f"/tmp/rescreen-manifest-{name}.json"
            with open(manifest_path, "w", encoding="utf-8") as fh:
                json.dump(manifest, fh, ensure_ascii=False)
            env = dict(os.environ)
            env["A2A_EDGE_SECRET"] = secret
            try:
                completed = m._run(
                    ["node", str(cfg.a2a_nexus_dir / "scripts" / "a2a-dispatch-round.mjs"),
                     "--manifest", manifest_path, "--verify", "--json"],
                    env=env, timeout=300)
                result = json.loads(completed.stdout.decode("utf-8"))
                trow = (result.get("results") or [{}])[0]
                entry_result["task"] = trow.get("taskId")
            except Exception as error:  # noqa: BLE001 — record and continue
                entry_result["outcome"] = "rescreen-dispatch-failed"
                entry_result["error"] = str(error)[:120]
        results.append(entry_result)

    summary = {"ok": True, "dryrun": bool(args.dry_run),
               "pool": [{k: v for k, v in p.items() if k != "rb"} for p in rotation],
               "exclusions": exclusions, "results": results}
    if args.output:
        pathlib.Path(args.output).write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                             encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    return 0


def tree_of(case: dict) -> str:
    return case.get("tree_sha256", "")


if __name__ == "__main__":
    sys.exit(main())
