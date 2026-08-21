#!/usr/bin/env python3
"""Weekly cost-ledger rollup into the wiki-candidates queue (#1205 stage 2 / D-4).

Aggregates one node's daily cost-ledger rows over the previous complete ISO
week (Mon 00:00 KST → Mon 00:00 KST) and appends ONE candidate entry to
$CCC_STATE_DIR/wiki-candidates.md.

Why the candidates queue: D-4 chose the wiki path over a2a for central
collection (issuecomment-5364641271). The queue is HUMAN-GATED by contract
(TM-1058: "Never auto-PR") — so this writer does NOT promote anything; it
queues one reviewable entry per (week, node), marked `class:
metrics/cost-rollup` so a reviewer can batch-promote machine entries with
/wiki-record, distinct from distill's prose candidates.

Idempotence: the entry embeds a `<!-- cost-rollup:YYYY-Www:NODE -->` marker;
a run that finds its marker already in the queue exits 0 without appending.

Usage:
  cost-ledger-weekly.py [--ledger PATH] [--week-start YYYY-MM-DD(KST)]
                        [--queue PATH] [--node NAME] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
DEFAULT_LEDGER = Path.home() / ".claude" / "state" / "cost-ledger.jsonl"
DEFAULT_QUEUE = Path(
    os.environ.get("CCC_STATE_DIR", str(Path.home() / ".claude" / "state"))
) / "wiki-candidates.md"
SUGGESTED_PATH = "pages/fleet/cost-ledger.md"
ENTRY_CLASS = "metrics/cost-rollup"


def _resolve_node() -> str:
    node = os.environ.get("CCC_NODE", "").strip()
    if node:
        return node
    state = os.environ.get("CCC_STATE_DIR", "").strip()
    if state:
        try:
            text = (Path(state) / "node.txt").read_text(encoding="utf-8").strip()
            if text:
                return text
        except OSError:
            pass
    import socket

    return socket.gethostname().split(".")[0]


def previous_iso_week(today_kst: datetime) -> tuple[datetime, datetime]:
    """Previous complete Mon→Mon week containing no part of this week."""
    monday = (today_kst - timedelta(days=today_kst.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return monday - timedelta(days=7), monday


def iso_week_label(day_kst: datetime) -> str:
    iso = day_kst.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def aggregate_week(ledger_path: Path, node: str, start_kst: datetime, end_kst: datetime) -> dict:
    """Sum the node's daily rows inside [start, end) per model."""
    models: dict[str, dict] = {}
    days_seen = 0
    if not ledger_path.is_file():
        return {"models": models, "days": 0}
    for line in ledger_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("node") != node:
            continue
        try:
            day = datetime.strptime(str(row.get("date", "")), "%Y-%m-%d").replace(tzinfo=KST)
        except ValueError:
            continue
        if not (start_kst <= day < end_kst):
            continue
        days_seen += 1
        for model, tok in (row.get("models") or {}).items():
            slot = models.setdefault(model, {
                "provider": tok.get("provider") or "claude",
                "turns": 0, "input_tokens": 0, "output_tokens": 0,
                "cache_read_input_tokens": 0, "cache_write_5m_tokens": 0,
                "cache_write_1h_tokens": 0, "cache_write_untyped_tokens": 0,
                "est_cost_usd": 0.0, "cost_null_days": 0,
            })
            slot["turns"] += int(tok.get("turns") or 0)
            for field in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                          "cache_write_5m_tokens", "cache_write_1h_tokens",
                          "cache_write_untyped_tokens"):
                slot[field] += int(tok.get(field) or 0)
            cost = tok.get("est_cost_usd")
            if cost is None:
                slot["cost_null_days"] += 1
            else:
                slot["est_cost_usd"] += float(cost)
    return {"models": models, "days": days_seen}


def next_cand_id(queue_text: str) -> int:
    ids = [int(m) for m in re.findall(r"^## \[CAND-(\d+)\]", queue_text, re.MULTILINE)]
    return (max(ids) + 1) if ids else 1


def build_entry(week_label: str, node: str, start_kst: datetime, agg: dict) -> str:
    marker = f"cost-rollup:{week_label}:{node}"
    lines_table = [
        "| model | provider | turns | input | output | cache_read | est_cost_usd |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    total_turns = 0
    total_cost = 0.0
    any_null = False
    for model in sorted(agg["models"]):
        m = agg["models"][model]
        total_turns += m["turns"]
        cost_cell: str
        if m["cost_null_days"]:
            any_null = True
            cost_cell = f"${m['est_cost_usd']:.2f}+null({m['cost_null_days']}d)"
        else:
            cost_cell = f"${m['est_cost_usd']:.2f}"
        total_cost += m["est_cost_usd"]
        lines_table.append(
            f"| {model} | {m['provider']} | {m['turns']} | {m['input_tokens']:,} "
            f"| {m['output_tokens']:,} | {m['cache_read_input_tokens']:,} | {cost_cell} |"
        )
    null_note = " (일부 모델 공식가 없음 — 토큰만 계상)" if any_null else ""
    summary = (
        f"{week_label} 주간 비용 원장 롤업({agg['days']}일분, KST 주 단위). "
        f"총 {total_turns}턴, 확정 비용 ${total_cost:.2f}{null_note}. "
        f"일일 원장: node cost-ledger.jsonl (#1205)."
    )
    return (
        f"<!-- {marker} -->\n"
        f"## [CAND-{{id}}] {week_label} — {node} 비용 원장 주간 롤업\n"
        f"- suggested-path: `{SUGGESTED_PATH}`\n"
        f"- proposed-id: assign at PR time\n"
        f"- class: `{ENTRY_CLASS}`\n"
        f"- source-session: `cost-ledger-weekly` (machine, node={node})\n"
        f"- distilled-at: {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
        f"- status: pending\n"
        f"- summary: {summary}\n"
        f"- evidence-excerpt: |\n"
        + "\n".join(f"    {line}" for line in lines_table)
        + "\n"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    ap.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    ap.add_argument("--week-start",
                    help="KST Monday YYYY-MM-DD of the week to roll up "
                         "(default: previous complete ISO week)")
    ap.add_argument("--node", default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    node = args.node or _resolve_node()

    if args.week_start:
        start_kst = datetime.strptime(args.week_start, "%Y-%m-%d").replace(tzinfo=KST)
        end_kst = start_kst + timedelta(days=7)
    else:
        start_kst, end_kst = previous_iso_week(datetime.now(KST))
    week_label = iso_week_label(start_kst)
    marker = f"cost-rollup:{week_label}:{node}"

    queue_text = args.queue.read_text(encoding="utf-8") if args.queue.is_file() else ""
    if marker in queue_text:
        print(json.dumps({"ok": True, "skipped": "already-queued",
                          "week": week_label, "node": node}))
        return 0

    agg = aggregate_week(args.ledger, node, start_kst, end_kst)
    if not agg["models"]:
        print(json.dumps({"ok": True, "skipped": "no-ledger-rows",
                          "week": week_label, "node": node}))
        return 0

    entry = build_entry(week_label, node, start_kst, agg)
    if args.dry_run:
        print(entry.replace("{id}", str(next_cand_id(queue_text))))
        return 0

    args.queue.parent.mkdir(parents=True, exist_ok=True)
    if not args.queue.is_file():
        args.queue.write_text(
            "# Wiki Candidates Queue (auto-generated by distill; review with `/wiki-record`)\n\n"
            "> Each entry is a durable operational fact / decision proposed by the Session\n"
            "> Distiller (TM-1058). Review and either:\n"
            ">   - Promote via `/wiki-record` (creates PR), then mark status: merged below.\n"
            ">   - Reject by deleting the entry (or marking status: rejected).\n>\n"
            "> Never auto-PR — this is a human-gated queue.\n\n",
            encoding="utf-8",
        )
        queue_text = args.queue.read_text(encoding="utf-8")
    entry = entry.replace("{id}", str(next_cand_id(queue_text)))
    with args.queue.open("a", encoding="utf-8") as fh:
        if queue_text and not queue_text.endswith("\n"):
            fh.write("\n")
        fh.write("\n" + entry)
    print(json.dumps({"ok": True, "queued": week_label, "node": node,
                      "days": agg["days"], "models": len(agg["models"])}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
