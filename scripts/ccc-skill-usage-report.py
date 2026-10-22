#!/usr/bin/env python3
"""Read-only per-node skill usage report and fleet aggregation (#2239, #1739).

Two subcommands:

    ccc-skill-usage-report.py node [--json] [--provider claude ...]
    ccc-skill-usage-report.py aggregate [--json] FILE... | -   (JSON lines)

``node`` lists every installed skill of this node with its origin
(repo-managed / fleet-managed / autosave / external / user / unknown), usage
counters and curator lifecycle state. Usage is the same ledger UNION the
curator judges idleness on (owner decision #1739): the Skill-tool record in
``state/skill-autosave-usage.json`` and the Read|Skill ledger
``state/skill-usage/usage.jsonl``. The ledger readers, record schema and
evidence rules are imported from claude/hooks/skill-review/curator.py so the
report can never disagree with the lifecycle about what "unused" means.

``aggregate`` merges several node reports (collect them with e.g.
``ssh <node> python3 <checkout>/scripts/ccc-skill-usage-report.py node --json``)
into archive candidates, graduation candidates, retirement candidates
(fleet-managed, never used on any of enough nodes — #2239 L1; the age rule is
applied by scripts/ccc-skill-retire.py from fleet-skills approval.json),
never-used skills and per-origin totals. Missing or unreadable usage telemetry never reads as
non-use: a node whose ledgers cannot back an idleness judgement disqualifies
the skills it holds from the archive/unused lists and is listed under
``evidence_gaps``.

Strictly read-only: nothing is moved, deleted or written; the output carries
counters, timestamps and classification labels only — never SKILL.md bodies
or descriptions.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import socket
import stat
import sys
from datetime import datetime, timedelta, timezone
import statistics
from types import ModuleType
from typing import Any, Iterable, TextIO

SCHEMA_VERSION = 1
NODE_KIND = "ccc-skill-usage-node-report"
AGGREGATE_KIND = "ccc-skill-usage-aggregate"
DEFAULT_WINDOW_DAYS = 30
DEFAULT_STALE_DAYS = 30
DEFAULT_GRADUATION_MIN_NODES = 2
DEFAULT_GRADUATION_MIN_USES = 3
DEFAULT_DWELL_DAYS = 14
# #2239 L1 (owner decision 2026-10-22, DOC-3903 P9): a fleet-managed skill never
# used on any of at least this many nodes is a retirement candidate for the
# fleet-skills `retired/` lane. The age rule (>= N days since `approved_at`)
# lives in scripts/ccc-skill-retire.py, which reads the canon repo: a node's
# `installed_at` is the fleet marker mtime, rewritten by every sync ref change,
# so it cannot anchor "days since distribution started".
DEFAULT_RETIRE_MIN_NODES = 3
MAX_REPORT_BYTES = 16 * 1024 * 1024
# #2283 lifecycle table: cap the per-node dwell sample list (body-free ints).
MAX_DWELL_SAMPLES = 5000
_ARCHIVE_KIND_STAGE = {
    "pending-90d": "archived:expired",
    "prescreen-reject": "archived:prescreen-reject",
    "draft-rejected": "archived:draft-rejected",
}
# #2373: an owner `/skillsuggest` reject (P3 human gate) is its own archive
# reason. `verdict:*` belongs to the P6 fleet intake review (publisher ledger),
# which never writes a node pending-skills index, so a node-index
# `verdict:reject` (written by #2283 before this fix, or reported by a node that
# has not updated yet) is the owner reject under its old name.
OWNER_REJECTED_STAGE = "archived:owner-rejected"
# #2374: an approved/installed draft is a node-local install. `approved` and
# `installed@<sha>` name the P6 promote merge / P7 fleet install, so the node
# index says local-installed:{auto|owner|manual}. `skill-review.sh reindex`
# tells owner (`autosave apply`) from manual (hand copy) by the draft's meta;
# a row or node report from before #2374 cannot, and counts as owner until
# that node's next reindex.
LOCAL_INSTALLED_AUTO = "local-installed:auto"
LOCAL_INSTALLED_OWNER = "local-installed:owner"
_NODE_INDEX_STAGE_ALIASES = {
    "verdict:reject": OWNER_REJECTED_STAGE,
    "approved": LOCAL_INSTALLED_OWNER,
    "installed": LOCAL_INSTALLED_AUTO,
}


def _node_index_stage(stage: str) -> str:
    """Map a pre-#2373/#2374 node-index stage name to its current name."""
    if stage.startswith("installed@"):
        return LOCAL_INSTALLED_AUTO
    return _NODE_INDEX_STAGE_ALIASES.get(stage, stage)

# ownership.py base classification -> report origin.
ORIGINS = {
    "managed/bundled": "repo-managed",
    "fleet-managed": "fleet-managed",
    "autosave-managed": "autosave",
    "external/repo-installed": "external",
    "user-owned": "user",
    "unknown/unreadable": "unknown",
}
ORIGIN_ORDER = ("repo-managed", "fleet-managed", "autosave", "external", "user", "unknown")
PROVIDERS = ("claude", "codex", "piri", "danso")


class ReportError(RuntimeError):
    """A body-free, closed-vocabulary failure code."""


def _load_curator() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "claude" / "hooks" / "skill-review" / "curator.py"
    spec = importlib.util.spec_from_file_location("ccc_skill_usage_report_curator", path)
    if spec is None or spec.loader is None:
        raise ReportError("curator_module_unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _ts(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        return parsed.astimezone(timezone.utc)
    except OverflowError:
        return None


def _latest(values: Iterable[datetime | None]) -> datetime | None:
    return max((value for value in values if value is not None), default=None)


def _earliest(values: Iterable[datetime | None]) -> datetime | None:
    return min((value for value in values if value is not None), default=None)


def _now() -> datetime:
    pinned = os.environ.get("CCC_SKILL_USAGE_REPORT_NOW", "")
    if pinned:
        parsed = _parse_ts(pinned)
        if parsed is None:
            raise ReportError("invalid_config_CCC_SKILL_USAGE_REPORT_NOW")
        return parsed
    return datetime.now(timezone.utc)


def _node_name() -> str:
    return os.environ.get("CCC_NODE", "").strip() or socket.gethostname()


# ---------------------------------------------------------------------------
# node
# ---------------------------------------------------------------------------


def _ledger_rows(curator: ModuleType, context: Any) -> tuple[str, list[tuple[str, str, datetime]]]:
    """(status, [(skill, runtime, ts)]) from usage.jsonl via the curator reader.

    Same safety bar and row filter as curator._read_usage_ledger: owner-only
    regular file, newest bytes only, malformed rows skipped. ``missing`` when
    no valid row exists (absence is never evidence of non-use).
    """
    path = context.state_dir / curator._READ_LEDGER_FILE
    status, data = curator._read_ledger_tail(path, context.uid)
    if status != "read":
        return status, []
    rows: list[tuple[str, str, datetime]] = []
    for line in data.splitlines():
        try:
            row = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(row, dict):
            continue
        name = row.get("skill")
        when = curator._parse_ts(row.get("ts"))
        if not isinstance(name, str) or not curator.ownership._NAME_RE.fullmatch(name) or when is None:
            continue
        runtime = row.get("runtime")
        rows.append((name, runtime if isinstance(runtime, str) else "claude", when))
    return ("present" if rows else "missing"), rows


def _skill_tool_usage(curator: ModuleType, context: Any) -> tuple[str, dict[str, Any]]:
    path = context.state_dir / curator._USAGE_FILE
    try:
        exists = curator.ownership._lstat(path) is not None
    except curator.ContractError:
        return "unreadable", {"schema_version": 1, "records": {}}
    if not exists:
        return "missing", {"schema_version": 1, "records": {}}
    try:
        return "present", curator._load_usage(context, strict=True)
    except curator.ContractError:
        return "unreadable", {"schema_version": 1, "records": {}}


def _provider_context(curator: ModuleType, provider: str) -> Any:
    args = argparse.Namespace(provider=provider, skills_dir=None, state_dir=None)
    return curator.ownership._build_context(args)


def _installed_at(context: Any, name: str, base: str) -> str | None:
    """Install time of the current copy: the fleet marker's mtime.

    The fleet-skills sync rewrites the marker on every install/update, so this
    is the CURRENT version's install time — an upper bound for the first
    install. Other origins carry no install stamp here.
    """
    if base != "fleet-managed":
        return None
    try:
        meta = os.lstat(context.skills_dir / name / ".ccc-fleet-skill.json")
    except OSError:
        return None
    if not stat.S_ISREG(meta.st_mode):
        return None
    return _ts(datetime.fromtimestamp(meta.st_mtime, timezone.utc))


def _node_skill(
    curator: ModuleType,
    context: Any,
    name: str,
    classification: dict[str, Any],
    usage: dict[str, Any],
    ledger: list[tuple[str, str, datetime]],
    window_start: datetime,
) -> dict[str, Any]:
    base = classification["base_classification"]
    record = usage["records"].get(curator._record_key(context, name))
    created = _parse_ts(record.get("created_at")) if record else None
    # Mirror curator._decide_union: a usage.jsonl row older than this skill's
    # record belongs to an earlier same-name skill and is not its use.
    rows = [when for skill, _rt, when in ledger if skill == name and (created is None or when >= created)]
    runtimes: dict[str, int] = {}
    for skill, runtime, when in ledger:
        if skill == name and (created is None or when >= created):
            runtimes[runtime] = runtimes.get(runtime, 0) + 1
    tool_count = record["use_count"] if record else 0
    tool_last = _parse_ts(record.get("last_used_at")) if record else None
    ledger_last = max(rows) if rows else None
    last_used = _latest((tool_last, ledger_last))
    window_rows = sum(1 for when in rows if when >= window_start)
    # The Skill-tool record has no per-event times: a last use inside the
    # window proves at least one windowed use, nothing more (lower bound).
    window_count = max(window_rows, 1 if tool_last is not None and tool_last >= window_start else 0)
    return {
        "name": name,
        "provider": context.provider,
        "origin": ORIGINS.get(base, "unknown"),
        "classification": base,
        "pinned": bool(classification.get("pinned")),
        # Every Skill-tool call also lands in usage.jsonl, so the two ledgers
        # overlap: the union count is the larger of the two, never their sum.
        "use_count": max(tool_count, len(rows)),
        "use_count_skill_tool": tool_count,
        "use_count_ledger": len(rows),
        "window_use_count": window_count,
        "runtimes": dict(sorted(runtimes.items())),
        "last_used_at": _ts(last_used),
        "created_at": record.get("created_at") if record else None,
        "installed_at": _installed_at(context, name, base),
        "curator_state": record["state"] if record else None,
        "stale_marked_at": record.get("stale_marked_at") if record else None,
    }


def _row_lifecycle(row: dict[str, Any]) -> str | None:
    """Lifecycle stage of one pending-skills index row (#2283).

    Rows written since #2283 carry ``lifecycle``; older rows are derived from
    ``status`` with the same rules ``skill-review.sh reindex`` uses."""
    stage = row.get("lifecycle")
    if isinstance(stage, str) and stage:
        return _node_index_stage(stage)
    status = row.get("status")
    if not isinstance(status, str):
        return None
    if status == "pending":
        verdict = row.get("prescreen")
        return f"prescreened:{verdict}" if isinstance(verdict, str) and verdict else "drafted"
    if status in ("approved", "installed"):
        return _node_index_stage(status)
    if status == "rejected":
        return OWNER_REJECTED_STAGE
    if status.startswith("archived:"):
        return _ARCHIVE_KIND_STAGE.get(status.split(":", 1)[1], status)
    return None


def _is_pending_stage(stage: str) -> bool:
    return stage == "drafted" or stage.startswith("prescreened:")


def _read_small_regular(path: Path) -> tuple[str, str | None]:
    """("ok", text) for a regular non-symlink file under the size cap."""
    try:
        meta = path.lstat()
    except FileNotFoundError:
        return "missing", None
    except OSError:
        return "unreadable", None
    if not stat.S_ISREG(meta.st_mode) or meta.st_size > MAX_REPORT_BYTES:
        return "unreadable", None
    try:
        return "ok", path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return "unreadable", None


def _node_lifecycle(state_dir: Path, now: datetime) -> dict[str, Any]:
    """Body-free skill lifecycle counters for this node (#2283, DOC-3903).

    Reads ``pending-skills/index.jsonl`` (rebuilt by every autosave sweep) and
    the fleet-skills ``installed.json`` receipt. Stage counts are per draft id
    (last row wins), pending dwell is whole days since ``staged_at`` for the
    drafts still undecided."""
    status, text = _read_small_regular(state_dir / "pending-skills" / "index.jsonl")
    latest: dict[str, dict[str, Any]] = {}
    malformed = 0
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            malformed += 1
            continue
        if not isinstance(row, dict):
            malformed += 1
            continue
        key = str(row.get("dir") or row.get("id") or "")
        if key:
            latest[key] = row
    stages: dict[str, int] = {}
    dwell: list[int] = []
    for row in latest.values():
        stage = _row_lifecycle(row)
        if stage is None:
            continue
        stages[stage] = stages.get(stage, 0) + 1
        if _is_pending_stage(stage):
            staged = _parse_ts(row.get("staged_at"))
            if staged is not None and len(dwell) < MAX_DWELL_SAMPLES:
                dwell.append(max(0, (now - staged).days))
    fleet_dir = Path(os.environ.get("CCC_FLEET_SKILLS_STATE_DIR", str(state_dir / "fleet-skills")))
    receipt_status, receipt_text = _read_small_regular(fleet_dir / "installed.json")
    installed = 0
    if receipt_status == "ok":
        try:
            receipt = json.loads(receipt_text or "")
            installed = len(receipt.get("skills") or []) if isinstance(receipt, dict) else 0
        except ValueError:
            receipt_status = "unreadable"
    return {
        "index": status,
        "rows": len(latest),
        "malformed_rows": malformed,
        "stages": dict(sorted(stages.items())),
        "pending_dwell_days": sorted(dwell),
        "fleet_installed": {"receipt": receipt_status, "skills": installed},
    }


def build_node_report(providers: Iterable[str], window_days: int) -> dict[str, Any]:
    curator = _load_curator()
    now = _now()
    window_start = now - timedelta(days=window_days)
    skills: list[dict[str, Any]] = []
    ledgers: dict[str, Any] = {}
    roots: dict[str, str] = {}
    state_dir: Path | None = None
    for provider in providers:
        try:
            context = _provider_context(curator, provider)
        except curator.ContractError as error:
            ledgers[provider] = {"error": error.code}
            continue
        roots[provider] = str(context.skills_dir)
        # The pending queue is node-global (ownership.py state anchor).
        state_dir = state_dir or Path(context.state_dir)
        tool_status, usage = _skill_tool_usage(curator, context)
        ledger_status, ledger = _ledger_rows(curator, context)
        evidence = curator._usage_evidence(usage, ledger_status)
        if tool_status == "unreadable":
            evidence = "unreadable"
        ledgers[provider] = {
            "skill_tool": tool_status,
            "usage_jsonl": ledger_status,
            "evidence": evidence,
        }
        try:
            names = curator.ownership._skill_names(context)
        except curator.ContractError as error:
            ledgers[provider]["skills_root"] = error.code
            continue
        controls = curator.ownership._preload_controls(context)
        for name in names:
            classification = curator.ownership._classification(context, name, controls=controls)
            entry = _node_skill(curator, context, name, classification, usage, ledger, window_start)
            entry["usage_evidence"] = evidence
            skills.append(entry)
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": NODE_KIND,
        "node": _node_name(),
        "generated_at": _ts(now),
        "window_days": window_days,
        "skills_roots": roots,
        "usage_ledgers": ledgers,
        "skills": skills,
        "lifecycle": _node_lifecycle(state_dir, now) if state_dir is not None else {"index": "no-context"},
    }


# ---------------------------------------------------------------------------
# aggregate
# ---------------------------------------------------------------------------


def _iter_json_documents(text: str) -> Iterable[Any]:
    """One JSON document per file, or JSON lines (one report per line)."""
    stripped = text.strip()
    if not stripped:
        return []
    try:
        return [json.loads(stripped)]
    except ValueError:
        pass
    documents = []
    for line in stripped.splitlines():
        if line.strip():
            try:
                documents.append(json.loads(line))
            except ValueError:
                raise ReportError("input_not_json") from None
    return documents


def _read_inputs(paths: list[str], stdin: TextIO) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    sources = paths or ["-"]
    for source in sources:
        if source == "-":
            text = stdin.read(MAX_REPORT_BYTES + 1)
        else:
            try:
                with open(source, encoding="utf-8") as handle:
                    text = handle.read(MAX_REPORT_BYTES + 1)
            except OSError:
                raise ReportError("input_unreadable") from None
        if len(text) > MAX_REPORT_BYTES:
            raise ReportError("input_too_large")
        for document in _iter_json_documents(text):
            if (
                not isinstance(document, dict)
                or document.get("kind") != NODE_KIND
                or document.get("schema_version") != SCHEMA_VERSION
                or not isinstance(document.get("node"), str)
                or not isinstance(document.get("skills"), list)
            ):
                raise ReportError("input_not_a_node_report")
            reports.append(document)
    if not reports:
        raise ReportError("no_node_reports")
    return reports


def _merge_node_entries(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """One node's view of a skill name across its provider roots.

    usage.jsonl is node-wide and name-keyed (the curator's union), so the
    same name in two provider roots must not be counted twice: take maxima.
    The claude root's origin wins; otherwise the first provider's.
    """
    entries = sorted(entries, key=lambda e: (e.get("provider") != "claude", str(e.get("provider"))))
    first = entries[0]
    return {
        "origin": first.get("origin", "unknown"),
        "providers": [e.get("provider") for e in entries],
        "use_count": max(int(e.get("use_count") or 0) for e in entries),
        "window_use_count": max(int(e.get("window_use_count") or 0) for e in entries),
        "last_used_at": _latest(_parse_ts(e.get("last_used_at")) for e in entries),
        "created_at": _earliest(_parse_ts(e.get("created_at")) for e in entries),
        "installed_at": _earliest(_parse_ts(e.get("installed_at")) for e in entries),
        "curator_state": first.get("curator_state"),
        "stale_marked_at": first.get("stale_marked_at"),
        "evidence_ok": all(e.get("usage_evidence") == "ok" for e in entries),
    }


def _latest_reports(reports: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Newest report per node (a re-collected node replaces its older snapshot)."""
    floor = datetime.min.replace(tzinfo=timezone.utc)
    latest: dict[str, dict[str, Any]] = {}
    for report in reports:
        held = latest.get(report["node"])
        if held is None or (_parse_ts(report.get("generated_at")) or floor) >= (
            _parse_ts(held.get("generated_at")) or floor
        ):
            latest[report["node"]] = report
    return latest


def _index_skills(
    latest: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, dict[str, Any]]], list[dict[str, Any]]]:
    """({skill: {node: merged view}}, evidence gaps)."""
    by_skill: dict[str, dict[str, dict[str, Any]]] = {}
    evidence_gaps: list[dict[str, Any]] = []
    for node, report in sorted(latest.items()):
        grouped: dict[str, list[dict[str, Any]]] = {}
        for entry in report["skills"]:
            if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                grouped.setdefault(entry["name"], []).append(entry)
        for name, entries in grouped.items():
            by_skill.setdefault(name, {})[node] = _merge_node_entries(entries)
        for provider, status in sorted((report.get("usage_ledgers") or {}).items()):
            if not isinstance(status, dict) or status.get("evidence") != "ok":
                evidence_gaps.append({"node": node, "provider": provider, "status": status})
    return by_skill, evidence_gaps


def _used(view: dict[str, Any]) -> bool:
    return view["use_count"] > 0 or view["last_used_at"] is not None


def _archive_candidate(
    name: str, nodes: dict[str, dict[str, Any]], now: datetime, stale_cutoff: datetime
) -> dict[str, Any] | None:
    """Autosave skill idle on EVERY node holding the name, whatever its origin.

    A use of the same skill anywhere is evidence of value. The anchor mirrors
    the curator (last use, else creation; a fleet copy without a record falls
    back to its install time); an unknown anchor is never idle.
    """
    if not any(v["origin"] == "autosave" for v in nodes.values()):
        return None
    anchors = {node: v["last_used_at"] or v["created_at"] or v["installed_at"] for node, v in nodes.items()}
    idle: dict[str, int] = {}
    for node, anchor in anchors.items():
        if anchor is None or anchor > stale_cutoff:
            return None
        idle[node] = max(0, (now - anchor).days)
    return {
        "name": name,
        "nodes": {
            node: {
                "origin": view["origin"],
                "idle_days": idle[node],
                "last_used_at": _ts(view["last_used_at"]),
                "curator_state": view["curator_state"],
                "stale_marked_at": view["stale_marked_at"],
            }
            for node, view in sorted(nodes.items())
        },
        "min_idle_days": min(idle.values()),
    }


def _graduation_candidate(
    name: str,
    nodes: dict[str, dict[str, Any]],
    *,
    min_nodes: int,
    min_uses: int,
    dwell_cutoff: datetime,
) -> dict[str, Any] | None:
    fleet_nodes = {node: v for node, v in nodes.items() if v["origin"] == "fleet-managed"}
    used_nodes = sorted(node for node, v in fleet_nodes.items() if v["window_use_count"] > 0)
    window_uses = sum(v["window_use_count"] for v in fleet_nodes.values())
    if not fleet_nodes or (len(used_nodes) < min_nodes and window_uses < min_uses):
        return None
    earliest = _earliest(v["installed_at"] for v in fleet_nodes.values())
    return {
        "name": name,
        "used_nodes": used_nodes,
        "installed_nodes": sorted(fleet_nodes),
        "window_use_count": window_uses,
        "use_count": sum(v["use_count"] for v in fleet_nodes.values()),
        "last_used_at": _ts(_latest(v["last_used_at"] for v in fleet_nodes.values())),
        "earliest_installed_at": _ts(earliest),
        # The marker mtime is the current version's install, an upper bound
        # for the first install: "met" proves >= dwell_days on a node;
        # "unconfirmed" is not a fail — check the intake PR merge date.
        "dwell": "met" if earliest is not None and earliest <= dwell_cutoff else "unconfirmed",
    }


def _retire_candidate(
    name: str,
    nodes: dict[str, dict[str, Any]],
    *,
    min_nodes: int,
) -> dict[str, Any] | None:
    """Fleet-managed on every holder, never used anywhere, enough holders.

    Owner decision 2026-10-22 (#2239 L1, DOC-3903 P9): a skill the fleet-skills
    sync installed on at least ``min_nodes`` nodes with no use recorded on any
    of them may be moved to the fleet-skills ``retired/`` lane. Any other
    origin on any node (an autosave original, a user copy) is value evidence
    of a different kind and keeps the name out. The caller has already
    required ``evidence_ok`` on every holder, so a telemetry gap never reads
    as non-use (#1739). Usage facts only: the node ``installed_at`` is the
    fleet marker's mtime, rewritten by every sync ref change, so the "days
    since distribution" rule is judged by ccc-skill-retire.py from the canon
    repo's ``approval.json`` ``approved_at``.
    """
    if len(nodes) < min_nodes:
        return None
    if any(v["origin"] != "fleet-managed" or not v["evidence_ok"] for v in nodes.values()):
        return None
    if any(_used(v) for v in nodes.values()):
        return None
    return {"name": name, "nodes": sorted(nodes), "holder_count": len(nodes)}


def _origin_totals(by_skill: dict[str, dict[str, dict[str, Any]]]) -> dict[str, dict[str, int]]:
    totals: dict[str, dict[str, int]] = {
        origin: {"skills": 0, "installs": 0, "used_installs": 0} for origin in ORIGIN_ORDER
    }
    for nodes in by_skill.values():
        for origin in {view["origin"] for view in nodes.values()}:
            totals.setdefault(origin, {"skills": 0, "installs": 0, "used_installs": 0})["skills"] += 1
        for view in nodes.values():
            totals[view["origin"]]["installs"] += 1
            totals[view["origin"]]["used_installs"] += int(_used(view))
    return totals


def _percentile(values: list[int], pct: float) -> int | None:
    """Nearest-rank percentile of a sorted list."""
    if not values:
        return None
    rank = max(1, -(-len(values) * pct // 100))
    return values[int(rank) - 1]


def _lifecycle_aggregate(latest: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Fleet lifecycle table (#2283): stage counts, pending dwell, archive reasons."""
    stages: dict[str, int] = {}
    dwell: list[int] = []
    per_node: dict[str, Any] = {}
    missing: list[str] = []
    for node, report in sorted(latest.items()):
        section = report.get("lifecycle")
        if not isinstance(section, dict) or section.get("index") != "ok":
            missing.append(node)
            if isinstance(section, dict):
                per_node[node] = {"index": section.get("index")}
            continue
        node_stages: dict[str, int] = {}
        for k, v in (section.get("stages") or {}).items():
            if isinstance(k, str) and isinstance(v, int) and v >= 0:
                k = _node_index_stage(k)
                node_stages[k] = node_stages.get(k, 0) + v
        node_dwell = sorted(
            d for d in (section.get("pending_dwell_days") or []) if isinstance(d, int) and d >= 0
        )
        for stage, count in node_stages.items():
            stages[stage] = stages.get(stage, 0) + count
        dwell.extend(node_dwell)
        installed = section.get("fleet_installed") if isinstance(section.get("fleet_installed"), dict) else {}
        per_node[node] = {
            "index": "ok",
            "drafts": sum(node_stages.values()),
            "pending": len(node_dwell),
            "pending_dwell_median_days": statistics.median(node_dwell) if node_dwell else None,
            "fleet_installed": installed.get("skills") if installed.get("receipt") == "ok" else None,
        }
    dwell.sort()
    return {
        "stages": dict(sorted(stages.items())),
        "archive_reasons": {k: v for k, v in sorted(stages.items()) if k.startswith("archived:")},
        "pending_dwell_days": {
            "count": len(dwell),
            "median": statistics.median(dwell) if dwell else None,
            "p90": _percentile(dwell, 90),
            "max": dwell[-1] if dwell else None,
        },
        "nodes": per_node,
        "nodes_without_index": missing,
    }


def build_aggregate(
    reports: list[dict[str, Any]],
    *,
    stale_days: int,
    min_nodes: int,
    min_uses: int,
    dwell_days: int,
    retire_min_nodes: int = DEFAULT_RETIRE_MIN_NODES,
) -> dict[str, Any]:
    latest = _latest_reports(reports)
    # "now" is the newest collection time, so a re-run over the same files is
    # deterministic; idle days are measured against it.
    now = _latest(_parse_ts(r.get("generated_at")) for r in latest.values()) or _now()
    by_skill, evidence_gaps = _index_skills(latest)
    archive_candidates: list[dict[str, Any]] = []
    graduation_candidates: list[dict[str, Any]] = []
    retire_candidates: list[dict[str, Any]] = []
    unused: list[dict[str, Any]] = []
    for name, nodes in sorted(by_skill.items()):
        graduation = _graduation_candidate(
            name, nodes, min_nodes=min_nodes, min_uses=min_uses,
            dwell_cutoff=now - timedelta(days=dwell_days),
        )
        if graduation is not None:
            graduation_candidates.append(graduation)
        # Telemetry gaps never read as non-use: such skills are neither idle
        # nor unused (#1739).
        if not all(view["evidence_ok"] for view in nodes.values()):
            continue
        archive = _archive_candidate(name, nodes, now, now - timedelta(days=stale_days))
        if archive is not None:
            archive_candidates.append(archive)
        if graduation is None:
            retire = _retire_candidate(name, nodes, min_nodes=retire_min_nodes)
            if retire is not None:
                retire_candidates.append(retire)
        if not any(_used(view) for view in nodes.values()):
            unused.append(
                {
                    "name": name,
                    "nodes": sorted(nodes),
                    "origins": sorted({view["origin"] for view in nodes.values()}),
                    "oldest_created_at": _ts(_earliest(v["created_at"] for v in nodes.values())),
                }
            )
    window_days = sorted(
        {r["window_days"] for r in latest.values() if isinstance(r.get("window_days"), int)}
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": AGGREGATE_KIND,
        "generated_at": _ts(now),
        "nodes": {node: r.get("generated_at") for node, r in sorted(latest.items())},
        "thresholds": {
            "stale_days": stale_days,
            "graduation_min_nodes": min_nodes,
            "graduation_min_uses": min_uses,
            "dwell_days": dwell_days,
            "retire_min_nodes": retire_min_nodes,
            "window_days": window_days,
        },
        "archive_candidates": archive_candidates,
        "graduation_candidates": graduation_candidates,
        "retire_candidates": retire_candidates,
        "unused": unused,
        "totals": {"by_origin": _origin_totals(by_skill), "skills": len(by_skill), "nodes": len(latest)},
        "evidence_gaps": evidence_gaps,
        "lifecycle": _lifecycle_aggregate(latest),
    }


# ---------------------------------------------------------------------------
# text rendering
# ---------------------------------------------------------------------------


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    cells = [[("-" if value is None else str(value)) for value in row] for row in rows]
    widths = [max([len(h)] + [len(r[i]) for r in cells]) for i, h in enumerate(headers)]
    lines = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip()]
    for row in cells:
        lines.append("  ".join(v.ljust(widths[i]) for i, v in enumerate(row)).rstrip())
    return "\n".join(lines)


def render_node(report: dict[str, Any]) -> str:
    out = [
        f"node {report['node']}  generated_at {report['generated_at']}  window {report['window_days']}d",
    ]
    for provider, status in report["usage_ledgers"].items():
        out.append(f"  {provider}: {json.dumps(status, sort_keys=True)}")
    rows = [
        [
            s["name"], s["provider"], s["origin"], s["use_count"], s["window_use_count"],
            s["last_used_at"], s["created_at"], s["curator_state"], s["stale_marked_at"],
        ]
        for s in report["skills"]
    ]
    out.append(
        _table(
            ["name", "provider", "origin", "uses", "uses/window", "last_used_at", "created_at", "curator", "stale_marked_at"],
            rows,
        )
    )
    return "\n".join(out) + "\n"


def render_aggregate(agg: dict[str, Any]) -> str:
    t = agg["thresholds"]
    out = [
        f"aggregate of {agg['totals']['nodes']} node(s), {agg['totals']['skills']} skill name(s)  generated_at {agg['generated_at']}",
        f"thresholds: stale>={t['stale_days']}d  graduation: >={t['graduation_min_nodes']} nodes or >={t['graduation_min_uses']} uses/window (window {t['window_days']}d)  dwell {t['dwell_days']}d",
        "",
        "per-origin totals",
        _table(
            ["origin", "skills", "installs", "used_installs"],
            [[o, v["skills"], v["installs"], v["used_installs"]] for o, v in agg["totals"]["by_origin"].items()],
        ),
        "",
        f"archive candidates (autosave, idle >= {t['stale_days']}d on every node holding them): {len(agg['archive_candidates'])}",
    ]
    if agg["archive_candidates"]:
        out.append(
            _table(
                ["name", "min_idle_days", "nodes (curator_state)"],
                [
                    [
                        c["name"], c["min_idle_days"],
                        ", ".join(f"{n}({v['curator_state'] or '-'})" for n, v in c["nodes"].items()),
                    ]
                    for c in agg["archive_candidates"]
                ],
            )
        )
    out += ["", f"graduation candidates (fleet-managed): {len(agg['graduation_candidates'])}"]
    if agg["graduation_candidates"]:
        out.append(
            _table(
                ["name", "used_nodes", "uses/window", "earliest_installed_at", "dwell"],
                [
                    [g["name"], ",".join(g["used_nodes"]) or "-", g["window_use_count"], g["earliest_installed_at"], g["dwell"]]
                    for g in agg["graduation_candidates"]
                ],
            )
        )
    retire = agg.get("retire_candidates") or []
    out += [
        "",
        f"retire candidates (fleet-managed on >= {t.get('retire_min_nodes', DEFAULT_RETIRE_MIN_NODES)} nodes, "
        f"never used anywhere; age judged by ccc-skill-retire.py from approval.json): {len(retire)}",
    ]
    if retire:
        out.append(_table(["name", "holders"], [[c["name"], c["holder_count"]] for c in retire]))
    out += ["", f"never used anywhere: {len(agg['unused'])}"]
    if agg["unused"]:
        out.append(
            _table(
                ["name", "origins", "nodes"],
                [[u["name"], ",".join(u["origins"]), ",".join(u["nodes"])] for u in agg["unused"]],
            )
        )
    lc = agg.get("lifecycle")
    if isinstance(lc, dict):
        d = lc["pending_dwell_days"]
        out += [
            "",
            "skill lifecycle (#2283, DOC-3903)",
            f"pending dwell: {d['count']} undecided draft(s)  median {d['median'] if d['median'] is not None else '-'}d"
            f"  p90 {d['p90'] if d['p90'] is not None else '-'}d  max {d['max'] if d['max'] is not None else '-'}d",
            _table(["stage", "drafts"], [[k, v] for k, v in lc["stages"].items()]) if lc["stages"] else "(no index rows)",
            "",
            f"archive reasons: {sum(lc['archive_reasons'].values())}",
        ]
        if lc["archive_reasons"]:
            out.append(_table(["reason", "drafts"], [[k, v] for k, v in lc["archive_reasons"].items()]))
        if lc["nodes"]:
            out += ["", "per node"]
            out.append(
                _table(
                    ["node", "index", "drafts", "pending", "dwell_median_d", "fleet_installed"],
                    [
                        [n, v.get("index"), v.get("drafts", "-"), v.get("pending", "-"),
                         v.get("pending_dwell_median_days") if v.get("pending_dwell_median_days") is not None else "-",
                         v.get("fleet_installed") if v.get("fleet_installed") is not None else "-"]
                        for n, v in lc["nodes"].items()
                    ],
                )
            )
        if lc["nodes_without_index"]:
            out.append(f"nodes without a readable pending index: {', '.join(lc['nodes_without_index'])}")
    if agg["evidence_gaps"]:
        out += ["", "evidence gaps (these nodes' skills are excluded from archive/unused lists):"]
        for gap in agg["evidence_gaps"]:
            out.append(f"  {gap['node']}/{gap['provider']}: {json.dumps(gap['status'], sort_keys=True)}")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _positive(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be an integer") from None
    if number < 1 or number > 3650:
        raise argparse.ArgumentTypeError("must be between 1 and 3650")
    return number


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only skill usage report (node) and fleet aggregation (aggregate)."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    node = sub.add_parser("node", help="report this node's installed skills")
    node.add_argument("--json", action="store_true", help="machine-readable output")
    node.add_argument(
        "--provider",
        action="append",
        choices=PROVIDERS,
        help="skills root(s) to report (repeatable; default: claude)",
    )
    node.add_argument(
        "--window-days", type=_positive, default=DEFAULT_WINDOW_DAYS,
        help=f"usage window for window_use_count (default {DEFAULT_WINDOW_DAYS})",
    )
    agg = sub.add_parser("aggregate", help="merge node reports (files, or JSON lines on stdin)")
    agg.add_argument("files", nargs="*", help="node report files; '-' or none reads stdin")
    agg.add_argument("--json", action="store_true", help="machine-readable output")
    agg.add_argument(
        "--stale-days", type=_positive, default=DEFAULT_STALE_DAYS,
        help=f"archive candidate idle threshold (default {DEFAULT_STALE_DAYS}, = curator STALE_AFTER_DAYS)",
    )
    agg.add_argument(
        "--graduation-min-nodes", type=_positive, default=DEFAULT_GRADUATION_MIN_NODES,
        help=f"fleet skill used on at least N nodes in the window (default {DEFAULT_GRADUATION_MIN_NODES})",
    )
    agg.add_argument(
        "--graduation-min-uses", type=_positive, default=DEFAULT_GRADUATION_MIN_USES,
        help=f"or at least N uses summed over nodes in the window (default {DEFAULT_GRADUATION_MIN_USES})",
    )
    agg.add_argument(
        "--dwell-days", type=_positive, default=DEFAULT_DWELL_DAYS,
        help=f"graduation dwell evidence threshold (default {DEFAULT_DWELL_DAYS}, docs/skill-graduation.md)",
    )
    agg.add_argument(
        "--retire-min-nodes", type=_positive, default=DEFAULT_RETIRE_MIN_NODES,
        help=f"retire candidate: held by at least N nodes (default {DEFAULT_RETIRE_MIN_NODES})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "node":
            report = build_node_report(args.provider or ["claude"], args.window_days)
            text = json.dumps(report, ensure_ascii=False, sort_keys=True) if args.json else render_node(report)
        else:
            reports = _read_inputs(args.files, sys.stdin)
            agg = build_aggregate(
                reports,
                stale_days=args.stale_days,
                min_nodes=args.graduation_min_nodes,
                min_uses=args.graduation_min_uses,
                dwell_days=args.dwell_days,
                retire_min_nodes=args.retire_min_nodes,
            )
            text = json.dumps(agg, ensure_ascii=False, sort_keys=True) if args.json else render_aggregate(agg)
    except ReportError as error:
        print(json.dumps({"ok": False, "code": str(error)}, sort_keys=True))
        return 2
    except Exception as error:  # noqa: BLE001 — body-free failure, never a traceback with paths
        code = getattr(error, "code", None) or type(error).__name__
        print(json.dumps({"ok": False, "code": str(code)}, sort_keys=True))
        return 2
    sys.stdout.write(text if text.endswith("\n") else text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
