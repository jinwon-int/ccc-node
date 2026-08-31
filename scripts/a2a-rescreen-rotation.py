#!/usr/bin/env python3
"""Provider-aware, state-aware rescreen rotation planner (a2a-nexus#2028).

Replaces the hand-written per-round rescreen scripts (seoseo /root/rescreen-*.py)
whose healthy-node lists were hardcoded and blind to node state. This tool:

  plan      — pure, deterministic planning from JSON inputs (no network): pick a
              reviewer per case from online workers, excluding the author node
              and nodes with recent failure evidence, balancing providers.
  probe     — live gather: broker online workers + per-node review provider
              (REVIEW_AGENT_* via SSH; x86 env file or Termux canonical env).
  manifests — optional: build dispatch manifests from a plan using the
              ccc-skill-promotion hook module. Without --dispatch it only
              writes manifests (the dry-run contract; no side effects).

Safety: plan never touches the network; probe is read-only; manifests without
--dispatch write files only. Dispatch (with --dispatch) reuses the existing
a2a-dispatch-round.mjs verify path and records rationale in the plan artifact.
"""
from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

DEFAULT_FAILURE_WINDOW_HOURS = 24


# ---------------------------------------------------------------- primitives


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, AttributeError, TypeError):
        return None


def load_json(path: str) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def dump_json(path: str, payload: Any) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def provider_from_env(bin_name: str, args: str) -> tuple[str, str]:
    """Derive (provider, model) from a worker's REVIEW_AGENT_BIN/ARGS strings."""
    b = (bin_name or "").strip().lower()
    a = (args or "").strip()
    if not b or b.endswith("claude") or b == "claude":
        return "claude", "claude-host-default"
    model = ""
    parts = a.split()
    for i, part in enumerate(parts):
        if part == "--model" and i + 1 < len(parts):
            model = parts[i + 1]
            break
    if not model:
        return "unknown", "unknown"
    provider = model.split("/", 1)[0] if "/" in model else model
    return provider, model


# ------------------------------------------------------------------- planning


def _normalize_workers(raw: Any) -> list[dict[str, Any]]:
    workers: list[dict[str, Any]] = []
    for item in raw or []:
        node = str(item.get("node", "")).strip()
        if not node:
            continue
        online = bool(item.get("online", False))
        provider = str(item.get("provider", "unknown")).strip().lower() or "unknown"
        model = str(item.get("model", "unknown")).strip() or "unknown"
        workers.append({"node": node, "provider": provider, "model": model, "online": online})
    workers.sort(key=lambda w: w["node"])
    return workers


def _failure_exclusions(
    workers: list[dict[str, Any]],
    failures: list[dict[str, Any]],
    *,
    now: datetime,
    window_hours: int,
) -> tuple[dict[str, str], dict[str, str]]:
    """Return ({node: exclusion_reason}, {node: last_failure_detail})."""
    window = timedelta(hours=window_hours)
    reasons: dict[str, str] = {}
    detail: dict[str, str] = {}
    known = {w["node"] for w in workers}
    for rec in failures or []:
        node = str(rec.get("node", "")).strip()
        if node not in known:
            continue
        ts = _parse_ts(str(rec.get("ts", "")))
        if ts is None or (now - ts) > window or ts > now:
            continue
        reason = str(rec.get("reason", "unspecified"))[:60]
        prev = detail.get(node)
        if prev is None or (ts is not None and str(rec.get("ts", "")) > prev):
            detail[node] = str(rec.get("ts", ""))
            reasons[node] = f"recent-failure:{reason}"
    return reasons, detail


def plan(
    cases: list[dict[str, Any]],
    workers: list[dict[str, Any]],
    failures: list[dict[str, Any]] | None = None,
    *,
    start_offset: int = 0,
    failure_window_hours: int = DEFAULT_FAILURE_WINDOW_HOURS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Deterministically assign a reviewer to each case.

    Exclusions (with recorded reasons): offline workers, workers with failure
    evidence inside the window. Eligibility: worker != case author node.
    Selection: provider-balanced round robin over the rotated candidate order;
    ties broken by node name so identical inputs yield identical assignments.
    """
    now = now or _utcnow()
    workers = _normalize_workers(workers)
    fail_reasons, _detail = _failure_exclusions(
        workers, failures or [], now=now, window_hours=failure_window_hours
    )

    excluded: dict[str, str] = {}
    for w in workers:
        if not w["online"]:
            excluded[w["node"]] = "offline"
    for node, reason in fail_reasons.items():
        excluded.setdefault(node, reason)

    online = [w for w in workers if w["online"]]
    candidates = [w["node"] for w in online if w["node"] not in excluded]
    if candidates and start_offset:
        offset = start_offset % len(candidates)
        candidates = candidates[offset:] + candidates[:offset]

    by_node = {w["node"]: w for w in workers}
    rot_index = {n: i for i, n in enumerate(candidates)}
    assigned_count: dict[str, int] = {}
    provider_count: dict[str, int] = {}
    assignments: list[dict[str, Any]] = []
    unassigned: list[dict[str, Any]] = []

    for case in cases or []:
        name = str(case.get("name", ""))
        author = str(case.get("author_node", "")).strip()
        eligible = [n for n in candidates if n != author]
        if not eligible:
            unassigned.append(
                {
                    "name": name,
                    "pr": case.get("pr"),
                    "reason": "rotation-exhausted",
                    "author_node": author,
                }
            )
            continue
        chosen = min(
            eligible,
            key=lambda n: (
                assigned_count.get(n, 0),
                provider_count.get(by_node[n]["provider"], 0),
                rot_index[n],
            ),
        )
        assigned_count[chosen] = assigned_count.get(chosen, 0) + 1
        prov = by_node[chosen]["provider"]
        provider_count[prov] = provider_count.get(prov, 0) + 1
        case_excluded = {n: reason for n, reason in excluded.items()}
        if author in by_node:
            case_excluded.setdefault(author, "author-exclusion")
        assignments.append(
            {
                "name": name,
                "pr": case.get("pr"),
                "branch": case.get("branch"),
                "author_node": author,
                "reviewer": chosen,
                "provider": prov,
                "model": by_node[chosen]["model"],
                "rationale": {
                    "selected_by": "provider-balance/round-robin",
                    "excluded": case_excluded,
                },
            }
        )

    return {
        "ok": True,
        "mode": "plan",
        "assignments": assignments,
        "unassigned": unassigned,
        "excluded_workers": [{"node": n, "reason": r} for n, r in sorted(excluded.items())],
        "candidates": candidates,
        "start_offset": start_offset,
        "failure_window_hours": failure_window_hours,
        "count": len(assignments),
    }


# ---------------------------------------------------------------- live probe


def _find_hook_module() -> pathlib.Path | None:
    """Locate the ccc-skill-promotion hook (probe/manifests need it)."""
    candidates = [
        pathlib.Path(os.environ.get("CCC_CLAUDE_DIR", str(pathlib.Path.home() / ".claude")))
        / "hooks"
        / "ccc-skill-promotion.py",
        pathlib.Path("/root/.claude/hooks/ccc-skill-promotion.py"),
        pathlib.Path("/opt/ccc-node/claude/hooks/ccc-skill-promotion.py"),
        pathlib.Path("/home/gongmyoung/ccc-node/claude/hooks/ccc-skill-promotion.py"),
    ]
    for path in candidates:
        if path.is_file():
            return path
    return None


def _load_hook_module(path: pathlib.Path):
    spec = importlib.util.spec_from_file_location("promo_rs_rotation", path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules["promo_rs_rotation"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _broker_online_workers(broker_url: str, edge_env_file: str) -> tuple[list[str], str | None]:
    """Query broker online worker ids (GET /workers with edge-secret header).

    The secret is sourced by bash from the edge env file and passed straight to
    curl — it never enters this process's Python data, so there is no sensitive
    dataflow through this module (CodeQL py/clear-text-logging eliminated by
    construction, not by suppression).
    Returns (sorted ids, error).
    """
    script = ('. "$EDGE_ENV"; '
              'curl -fsS -H "x-a2a-edge-secret: $A2A_EDGE_SECRET" "$BROKER_URL/workers"')
    try:
        proc = subprocess.run(
            ["bash", "-c", script, "_", edge_env_file, broker_url],
            capture_output=True,
            timeout=25,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return [], "broker-unreachable"
    if proc.returncode != 0:
        return [], "broker-unreachable"
    try:
        payload = json.loads(proc.stdout.decode("utf-8"))
    except json.JSONDecodeError:
        return [], "broker-unreachable"
    items = payload.get("items") if isinstance(payload, dict) else None
    online: set[str] = set()
    for row in items or []:
        node_id = row.get("nodeId") if isinstance(row, dict) else None
        if isinstance(node_id, str) and node_id.strip():
            online.add(node_id.strip())
    return sorted(online), None


def _ssh_probe_provider(node: str) -> dict[str, Any] | None:
    """SSH-read a node's REVIEW_AGENT_* wiring (read-only)."""
    remote = (
        "grep -hE '^REVIEW_AGENT_BIN=|^REVIEW_AGENT_ARGS=' /etc/default/a2a-hermes-worker 2>/dev/null; "
        "grep -hE '^REVIEW_AGENT_BIN=|^REVIEW_AGENT_ARGS=' ~/.a2a/*canonical*.env 2>/dev/null"
    )
    try:
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=12", node, remote],
            capture_output=True,
            timeout=25,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    bin_name, args = "", ""
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if line.startswith("REVIEW_AGENT_BIN="):
            bin_name = line.split("=", 1)[1].strip().strip('"')
        elif line.startswith("REVIEW_AGENT_ARGS="):
            args = line.split("=", 1)[1].strip().strip('"')
    if not proc.stdout and proc.returncode != 0:
        return None
    provider, model = provider_from_env(bin_name, args)
    return {"bin": bin_name or "claude(default)", "args": args, "provider": provider, "model": model}


def cmd_probe(args: argparse.Namespace) -> int:
    online, error = _broker_online_workers(args.broker_url, args.edge_env_file)
    if error == "edge-secret-missing":
        print(json.dumps({"ok": False, "error": error, "env_file": args.edge_env_file}))
        return 1
    if error:
        print(json.dumps({"ok": False, "error": error}))
        return 1
    workers: list[dict[str, Any]] = []
    nodes = [n.strip() for n in args.nodes.split(",") if n.strip()] if args.nodes else []
    for node in nodes:
        info = _ssh_probe_provider(node)
        workers.append(
            {
                "node": node,
                "online": node in online,
                "provider": (info or {}).get("provider", "unknown"),
                "model": (info or {}).get("model", "unknown"),
                "probe": (info or {}).get("bin", "unreachable"),
            }
        )
    payload = {
        "ok": True,
        "mode": "probe",
        "broker_url": args.broker_url,
        "online_workers": online,
        "workers": workers,
    }
    if args.out:
        dump_json(args.out, payload)
    print(json.dumps(payload, ensure_ascii=False))
    return 0


# ------------------------------------------------------- manifest emission


def cmd_manifests(args: argparse.Namespace) -> int:
    hook_path = _find_hook_module()
    if hook_path is None:
        print(json.dumps({"ok": False, "error": "promotion-hook-not-found"}))
        return 1
    hook = _load_hook_module(hook_path)
    secret = os.environ.get("A2A_EDGE_SECRET", "")
    if not secret:
        print(json.dumps({"ok": False, "error": "edge-secret-missing", "env": "A2A_EDGE_SECRET"}))
        return 1
    env = dict(os.environ)
    env.setdefault("CCC_NODE", "seoseo")
    cfg = hook._config(env)

    plan_data = load_json(args.plan)
    cases = {str(c.get("name")): c for c in load_json(args.cases)}
    out_dir = pathlib.Path(args.manifest_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    broker_id = hook._broker_id(cfg, secret)
    procedure = hook._worker_procedure_from_docs(
        cfg.a2a_nexus_dir, doc_name="skills-intake-review.md", end_marker=hook._DISPATCH_DOC_END
    )

    written: list[dict[str, str]] = []
    seq = 0
    for item in plan_data.get("assignments", []):
        case = cases.get(str(item.get("name")))
        if not case:
            continue
        seq += 1
        files = tuple(
            hook.SnapshotFile(
                relative=f["path"],
                content=base64.b64decode(f["content_b64"]),
                executable=bool(f.get("executable", False)),
            )
            for f in case["files"]
        )
        cand = hook.Candidate(
            node=case["node"],
            provider=case["provider"],
            name=case["name"],
            skill_sha256=case["skill_sha256"],
            tree_sha256=case["tree_sha256"],
            source_dir=pathlib.Path("/tmp"),
            files=files,
            description="",
        )
        head = hook._branch_head_sha(cfg, case["branch"])
        now = hook._utcnow().strftime("%Y%m%dT%H%M%SZ") + f"{seq:02d}"
        manifest = hook._build_dispatch_manifest(
            cfg,
            cand,
            pr_number=str(case["pr"]),
            branch=case["branch"],
            head=head,
            reviewer=str(item["reviewer"]),
            broker_id=broker_id,
            procedure=procedure,
            inventory=[],
            now=now,
        )
        manifest["lanes"][0]["payload"]["scope"] = "public-elevation"
        path = out_dir / f"rs-{case['name']}.json"
        dump_json(str(path), manifest)
        written.append({"name": case["name"], "reviewer": str(item["reviewer"]), "manifest": str(path)})
        print(json.dumps({"name": case["name"], "reviewer": item["reviewer"], "manifest": str(path)}), flush=True)

    if not args.dispatch:
        print(json.dumps({"ok": True, "mode": "manifests-dryrun", "written": len(written)}))
        return 0
    results = []
    for entry in written:
        try:
            completed = hook._run(
                [
                    "node",
                    str(cfg.a2a_nexus_dir / "scripts" / "a2a-dispatch-round.mjs"),
                    "--manifest",
                    entry["manifest"],
                    "--verify",
                    "--json",
                ],
                env={**env, "A2A_EDGE_SECRET": secret},
                timeout=300,
            )
            result = json.loads(completed.stdout.decode("utf-8"))
            trow = result.get("results", [{}])[0]
            results.append(
                {
                    "name": entry["name"],
                    "task": trow.get("taskId"),
                    "classification": trow.get("classification"),
                }
            )
        except Exception as exc:  # noqa: BLE001 — record and continue, rescreen semantics
            results.append({"name": entry["name"], "outcome": "dispatch-failed", "error": str(exc)[:120]})
        print(json.dumps(results[-1]), flush=True)
    print(json.dumps({"ok": True, "mode": "manifests-dispatch", "count": len(results)}))
    return 0


# -------------------------------------------------------------------- CLI


def cmd_plan(args: argparse.Namespace) -> int:
    cases = load_json(args.cases)
    workers = load_json(args.workers)
    failures = load_json(args.failures) if args.failures else None
    now = _parse_ts(args.now) if args.now else None
    result = plan(
        cases,
        workers,
        failures,
        start_offset=args.start_offset,
        failure_window_hours=args.failure_window_hours,
        now=now,
    )
    if args.out:
        dump_json(args.out, result)
    print(json.dumps(result, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    sub = parser.add_subparsers(dest="command", required=True)

    pp = sub.add_parser("plan", help="deterministic rotation plan from JSON inputs")
    pp.add_argument("--cases", required=True, help="cases JSON: [{name,pr,branch,author_node}]")
    pp.add_argument("--workers", required=True, help="workers JSON: [{node,provider,model,online}]")
    pp.add_argument("--failures", help="optional failures JSON: [{node,ts,reason,task_id}]")
    pp.add_argument("--failure-window-hours", type=int, default=DEFAULT_FAILURE_WINDOW_HOURS)
    pp.add_argument("--start-offset", type=int, default=0)
    pp.add_argument("--now", help="override current time (ISO 8601, for tests)")
    pp.add_argument("--out", help="also write the plan JSON here")

    pb = sub.add_parser("probe", help="live gather: broker online workers + node providers")
    pb.add_argument("--broker-url", required=True)
    pb.add_argument("--nodes", help="comma-separated node names to SSH-probe")
    pb.add_argument("--edge-env-file", default="/root/.a2a-broker-edge.env",
                    help="env file exporting A2A_EDGE_SECRET (sourced by bash, not read by python)")
    pb.add_argument("--out", help="write workers.json here")

    pm = sub.add_parser("manifests", help="build (and optionally dispatch) manifests from a plan")
    pm.add_argument("--plan", required=True)
    pm.add_argument("--cases", required=True)
    pm.add_argument("--manifest-dir", default="/tmp/intake-dispatch")
    pm.add_argument("--dispatch", action="store_true", help="actually dispatch (default: dry-run)")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "plan":
        return cmd_plan(args)
    if args.command == "probe":
        return cmd_probe(args)
    if args.command == "manifests":
        return cmd_manifests(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
