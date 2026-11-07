#!/usr/bin/env python3
"""Nightly span rollup: one node's KST day of bridge spans → one summary (#2684 slice 3).

Reads the node-local span ledger the bridge writes
(``<project>/.telegram_bot/spans/YYYY-MM-DD.jsonl``, ``bridge/core/span_ledger.py``)
for one KST day and produces a per-node daily summary:

* per tool (``kind=tool``): call count, error rate, p50/p95 ``duration_ms``;
* per model (``kind=turn``): turns, input/output tokens, cost — cross-checked
  against the bridge cost ledger (``.telegram_bot/usage-cost-ledger.jsonl``)
  for the same window, with a discrepancy % per comparable metric (issue
  target < 5 %);
* turn outcome counts (``status`` / ``ccc.outcome`` / ``error.type``) and
  ``ccc.skill`` use counts when present;
* A2A adapter spans (``kind=a2a``): ok/failure counts and the top-5 failure
  classes — only when such spans exist, otherwise omitted.

Delivery (same paths as the weekly cost rollup, #1227 / D-4):

1. one candidate entry appended to ``$CCC_STATE_DIR/wiki-candidates.md``
   (class ``metrics/span-rollup``; the queue is human-gated, never auto-PR);
2. one owner-only push-spool record (``event=SpansRollup``, ``tier=info``) in
   the bridge push spool, which the fleet-alerts relay holds for its daily
   09:00 KST 「🔔 플릿 알림」 digest. The spool file name is written into the
   candidate entry as delivery evidence.

Body-free: every span is passed through the canonical ``sanitize_span``
allow-list from ``bridge/core/span_ledger.py`` before it is counted, so the
summary can only contain aggregates and identifier-shaped values that the
ledger itself is allowed to store (tool names, model ids, outcome codes, …).
Unknown keys — including any text field a broken writer might add — are
dropped before aggregation. The cost ledger contributes numbers only.

Robustness: a missing spans directory, an empty day, unreadable files and
corrupt/torn lines never fail the run. An empty day exits 0 with a "no spans"
summary on stdout and delivers nothing (same as the weekly rollup's
``no-ledger-rows``: an idle node should not fill the human-gated queue; node
liveness is the doctor "last rollup tick" follow-up). Idempotence: the entry
embeds ``<!-- spans-rollup:YYYY-MM-DD:NODE -->``; a run that finds it in the
queue exits 0 without delivering again.

Usage:
  spans-rollup.py [--date YYYY-MM-DD(KST)] [--project-root DIR]
                  [--spans-dir DIR] [--cost-ledger PATH] [--queue PATH]
                  [--node NAME] [--no-digest] [--dry-run] [--json]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import secrets
import sys
import types
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

KST = timezone(timedelta(hours=9))
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
SUGGESTED_PATH = "pages/fleet/span-rollup.md"
ENTRY_CLASS = "metrics/span-rollup"
NOTIFY_EVENT = "SpansRollup"
#: Issue #2684 completion target for span totals vs the cost ledger.
DISCREPANCY_TARGET_PCT = 5.0
TOP_TOOLS_IN_ENTRY = 20
TOP_A2A_CLASSES = 5
#: A day file larger than this is not read (a sane day is a few MB at most).
MAX_DAY_FILE_BYTES = 256 * 1024 * 1024
UNNAMED = "(unnamed)"


# --- canonical span sanitizer -------------------------------------------------


def load_span_ledger(repo_root: Path = REPO_ROOT) -> Any:
    """Load ``bridge/core/span_ledger.py`` from this checkout.

    The bridge is a setuptools package-dir mapping (``bridge`` → ``telegram_bot``),
    so an unpackaged checkout cannot import it by name; span_ledger's only
    in-package dependency is ``telegram_bot.utils.redaction`` (stdlib only).
    When that is not importable, the checkout's redaction module is loaded by
    path and exposed under its package name just for the duration of the
    load, then the shim entries are removed again so nothing else in the
    process sees a fake ``telegram_bot`` package.
    """
    core = repo_root / "bridge" / "core" / "span_ledger.py"
    redaction_src = repo_root / "bridge" / "utils" / "redaction.py"
    shim: dict[str, types.ModuleType] = {}
    try:
        import telegram_bot.utils.redaction  # noqa: F401
    except Exception:
        spec = importlib.util.spec_from_file_location("_ccc_spans_rollup_redaction", redaction_src)
        if spec is None or spec.loader is None:
            raise RuntimeError("canonical redaction module unavailable")
        redaction = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(redaction)
        pkg = types.ModuleType("telegram_bot")
        pkg.__path__ = []
        utils = types.ModuleType("telegram_bot.utils")
        utils.__path__ = []
        shim = {"telegram_bot": pkg, "telegram_bot.utils": utils,
                "telegram_bot.utils.redaction": redaction}
        for name, module in shim.items():
            sys.modules.setdefault(name, module)
    try:
        spec = importlib.util.spec_from_file_location("_ccc_spans_rollup_span_ledger", core)
        if spec is None or spec.loader is None:
            raise RuntimeError("canonical span ledger unavailable")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for name, mod in shim.items():
            if sys.modules.get(name) is mod:
                del sys.modules[name]


# --- day window + readers -----------------------------------------------------


def resolve_node() -> str:
    """Same order as cost-ledger-weekly.py: CCC_NODE, state/node.txt, hostname."""
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

    return socket.gethostname().split(".")[0] or "node"


def kst_day_window(day: date) -> tuple[float, float]:
    start = datetime(day.year, day.month, day.day, tzinfo=KST)
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


def utc_files_for_window(spans_dir: Path, start: float, end: float) -> list[Path]:
    """The UTC day files a KST day overlaps (the ledger names files by UTC day)."""
    first = datetime.fromtimestamp(start, tz=timezone.utc).date()
    last = datetime.fromtimestamp(end - 0.001, tz=timezone.utc).date()
    out = []
    day = first
    while day <= last:
        out.append(spans_dir / f"{day.isoformat()}.jsonl")
        day += timedelta(days=1)
    return out


def _iter_lines(path: Path, stats: dict) -> Iterable[bytes]:
    try:
        if path.is_symlink() or not path.is_file():
            return
        if path.stat().st_size > MAX_DAY_FILE_BYTES:
            stats["oversize_files"] += 1
            return
        with path.open("rb") as handle:
            stats["files_read"] += 1
            yield from handle
    except OSError:
        stats["unreadable_files"] += 1


def read_spans(
    paths: Iterable[Path], start: float, end: float, sanitize: Callable[[object], Optional[dict]]
) -> tuple[list[dict], dict]:
    """Sanitized spans with ``start <= start_ts < end``; corrupt lines are counted, not raised."""
    stats = {"files_read": 0, "unreadable_files": 0, "oversize_files": 0,
             "corrupt_lines": 0, "rejected_spans": 0, "outside_window": 0}
    spans: list[dict] = []
    for path in paths:
        for raw in _iter_lines(path, stats):
            if not raw.strip():
                continue
            try:
                record = json.loads(raw)
            except ValueError:
                stats["corrupt_lines"] += 1
                continue
            clean = sanitize(record)
            if clean is None:
                stats["rejected_spans"] += 1
                continue
            ts = clean.get("start_ts")
            if not isinstance(ts, (int, float)) or not start <= ts < end:
                stats["outside_window"] += 1
                continue
            spans.append(clean)
    return spans, stats


def _num(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    number = float(value)
    return number if math.isfinite(number) and number >= 0 else 0.0


def _ledger_row_totals(record: dict) -> tuple[float, float, float]:
    tin = tout = 0.0
    models = record.get("models")
    for model_row in models if isinstance(models, list) else ():
        if isinstance(model_row, dict):
            tin += _num(model_row.get("in"))
            tout += _num(model_row.get("out"))
    return tin, tout, _num(record.get("total_cost_usd"))


def read_cost_ledger(path: Path, start: float, end: float) -> Optional[dict]:
    """Numeric totals of the bridge cost ledger inside the window (no names kept)."""
    if path.is_symlink() or not path.is_file():
        return None
    totals = {"turns": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
              "corrupt_lines": 0}
    try:
        with path.open("rb") as handle:
            for raw in handle:
                if not raw.strip():
                    continue
                try:
                    record = json.loads(raw)
                    ts = float(record.get("ts"))
                    if not isinstance(record.get("models"), list):
                        raise ValueError("models")
                except (ValueError, TypeError, AttributeError):
                    totals["corrupt_lines"] += 1
                    continue
                if not start <= ts < end:
                    continue
                tin, tout, cost = _ledger_row_totals(record)
                totals["turns"] += 1
                totals["input_tokens"] += int(tin)
                totals["output_tokens"] += int(tout)
                totals["cost_usd"] += cost
    except OSError:
        return None
    totals["cost_usd"] = round(totals["cost_usd"], 6)
    return totals


# --- aggregation --------------------------------------------------------------


def percentile(values: list[int], pct: float) -> Optional[float]:
    """Linear-interpolated percentile (numpy's default / Excel PERCENTILE.INC)."""
    if not values:
        return None
    data = sorted(values)
    if len(data) == 1:
        return float(data[0])
    rank = (len(data) - 1) * pct / 100.0
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return float(data[low])
    return data[low] + (data[high] - data[low]) * (rank - low)


def _rate(part: int, whole: int) -> float:
    return round(100.0 * part / whole, 2) if whole else 0.0


def aggregate_tools(spans: list[dict]) -> dict:
    tools: dict[str, dict] = {}
    for span in spans:
        if span.get("kind") != "tool":
            continue
        name = span.get("gen_ai.tool.name") or UNNAMED
        slot = tools.setdefault(name, {"calls": 0, "errors": 0, "durations": []})
        slot["calls"] += 1
        if span.get("status") != "ok":
            slot["errors"] += 1
        if isinstance(span.get("duration_ms"), int):
            slot["durations"].append(span["duration_ms"])
    out = {}
    for name, slot in tools.items():
        p50 = percentile(slot["durations"], 50)
        p95 = percentile(slot["durations"], 95)
        out[name] = {
            "calls": slot["calls"],
            "errors": slot["errors"],
            "error_rate_pct": _rate(slot["errors"], slot["calls"]),
            "p50_ms": None if p50 is None else round(p50, 1),
            "p95_ms": None if p95 is None else round(p95, 1),
        }
    return dict(sorted(out.items(), key=lambda kv: (-kv[1]["calls"], kv[0])))


def _empty_model_slot() -> dict:
    return {"turns": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
            "cache_creation_tokens": 0, "cost_usd": 0.0}


def aggregate_models(turns: list[dict]) -> dict:
    models: dict[str, dict] = {}
    for span in turns:
        slot = models.setdefault(span.get("gen_ai.request.model") or UNNAMED, _empty_model_slot())
        slot["turns"] += 1
        slot["input_tokens"] += int(span.get("gen_ai.usage.input_tokens") or 0)
        slot["output_tokens"] += int(span.get("gen_ai.usage.output_tokens") or 0)
        slot["cache_read_tokens"] += int(span.get("ccc.usage.cache_read_tokens") or 0)
        slot["cache_creation_tokens"] += int(span.get("ccc.usage.cache_creation_tokens") or 0)
        slot["cost_usd"] += float(span.get("ccc.cost_usd") or 0.0)
    for slot in models.values():
        slot["cost_usd"] = round(slot["cost_usd"], 6)
    return dict(sorted(models.items(), key=lambda kv: (-kv[1]["turns"], kv[0])))


def _count(spans: Iterable[dict], key: Callable[[dict], Optional[str]]) -> dict:
    counts: dict[str, int] = {}
    for span in spans:
        value = key(span)
        if value:
            counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def aggregate_a2a(spans: list[dict]) -> Optional[dict]:
    a2a = [s for s in spans if s.get("kind") == "a2a"]
    if not a2a:
        return None
    failures = [s for s in a2a if s.get("status") != "ok"]
    classes = _count(failures, lambda s: s.get("error.type") or s.get("ccc.outcome") or s["status"])
    return {
        "spans": len(a2a),
        "ok": len(a2a) - len(failures),
        "failed": len(failures),
        "top_failure_classes": dict(list(classes.items())[:TOP_A2A_CLASSES]),
    }


def _pct_diff(span_value: float, ledger_value: float) -> Optional[float]:
    if ledger_value == 0:
        return 0.0 if span_value == 0 else None
    return round(100.0 * abs(span_value - ledger_value) / ledger_value, 2)


def cross_check(turns: list[dict], ledger: Optional[dict]) -> dict:
    """Span turn totals vs the cost ledger for the same window.

    The ledger writes a row only for a turn that carries cost, so the span side
    counts turns with ``ccc.cost_usd > 0``. Its ``in`` excludes cache tokens
    while ``gen_ai.usage.input_tokens`` includes them, so the cache share is
    subtracted before comparing. The ledger is per model and a turn span names
    one model, so the comparison is on node totals, not per model.
    """
    if ledger is None:
        return {"available": False, "reason": "cost ledger missing or unreadable"}
    priced = [t for t in turns if float(t.get("ccc.cost_usd") or 0) > 0]
    span_side = {
        "turns": len(priced),
        "input_tokens": sum(
            max(0, int(t.get("gen_ai.usage.input_tokens") or 0)
                - int(t.get("ccc.usage.cache_read_tokens") or 0)
                - int(t.get("ccc.usage.cache_creation_tokens") or 0))
            for t in priced),
        "output_tokens": sum(int(t.get("gen_ai.usage.output_tokens") or 0) for t in priced),
        "cost_usd": round(sum(float(t.get("ccc.cost_usd") or 0) for t in priced), 6),
    }
    metrics = {}
    for name in ("turns", "input_tokens", "output_tokens", "cost_usd"):
        metrics[name] = {"spans": span_side[name], "ledger": ledger[name],
                         "diff_pct": _pct_diff(span_side[name], ledger[name])}
    diffs = [m["diff_pct"] for m in metrics.values()]
    worst = None if any(d is None for d in diffs) else max(diffs)
    return {
        "available": True,
        "metrics": metrics,
        "max_diff_pct": worst,
        "target_pct": DISCREPANCY_TARGET_PCT,
        "within_target": worst is not None and worst < DISCREPANCY_TARGET_PCT,
        "ledger_corrupt_lines": ledger.get("corrupt_lines", 0),
        "note": "node totals; ledger counts priced turns only, input excludes cache",
    }


def build_summary(day: date, node: str, spans: list[dict], read_stats: dict,
                  ledger: Optional[dict]) -> dict:
    turns = [s for s in spans if s.get("kind") == "turn"]
    tools = [s for s in spans if s.get("kind") == "tool"]
    summary: dict[str, Any] = {
        "schema": "ccc.spans-rollup.v1",
        "date": day.isoformat(),
        "tz": "Asia/Seoul",
        "node": node,
        "empty": not spans,
        "totals": {
            "spans": len(spans),
            "turns": len(turns),
            "tool_calls": len(tools),
            "tool_errors": sum(1 for s in tools if s.get("status") != "ok"),
            "by_kind": _count(spans, lambda s: s.get("kind")),
            "by_channel": _count(spans, lambda s: s.get("ccc.channel")),
            "by_tool_scope": _count(tools, lambda s: s.get("ccc.tool.scope")),
        },
        "tools": aggregate_tools(spans),
        "models": aggregate_models(turns),
        "turn_outcomes": {
            "status": _count(turns, lambda s: s.get("status")),
            "outcome": _count(turns, lambda s: s.get("ccc.outcome")),
            "error_type": _count(turns, lambda s: s.get("error.type")),
        },
        "skills": _count(spans, lambda s: s.get("ccc.skill")),
        "cost_ledger_check": cross_check(turns, ledger),
        "read": read_stats,
    }
    summary["totals"]["tool_error_rate_pct"] = _rate(
        summary["totals"]["tool_errors"], summary["totals"]["tool_calls"])
    a2a = aggregate_a2a(spans)
    if a2a is not None:
        summary["a2a"] = a2a
    return summary


# --- rendering ----------------------------------------------------------------


def _fmt_ms(value: Optional[float]) -> str:
    return "-" if value is None else f"{value:,.0f}"


def _fmt_pct(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:.1f}%"


def _check_line(check: Mapping[str, Any]) -> str:
    if not check.get("available"):
        return f"원장 교차검증: 불가 ({check.get('reason')})"
    worst = check.get("max_diff_pct")
    mark = "OK" if check.get("within_target") else "⚠"
    return (f"원장 교차검증: 최대 차이 {_fmt_pct(worst)} "
            f"(목표 <{check['target_pct']:.0f}%) {mark}")


def _table(header: list[str], rows: Iterable[list[str]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |",
             "|" + "|".join("---" if i == 0 else "---:" for i in range(len(header))) + "|"]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return lines


def _tools_table(summary: dict) -> list[str]:
    rows = [[name, str(t["calls"]), str(t["errors"]), f"{t['error_rate_pct']:.1f}%",
             _fmt_ms(t["p50_ms"]), _fmt_ms(t["p95_ms"])]
            for name, t in list(summary["tools"].items())[:TOP_TOOLS_IN_ENTRY]]
    return _table(["tool", "calls", "errors", "error_rate", "p50_ms", "p95_ms"], rows)


def _models_table(summary: dict) -> list[str]:
    rows = [[name, str(m["turns"]), f"{m['input_tokens']:,}", f"{m['output_tokens']:,}",
             f"{m['cache_read_tokens']:,}", f"${m['cost_usd']:.2f}"]
            for name, m in summary["models"].items()]
    return _table(["model", "turns", "input", "output", "cache_read", "cost_usd"], rows)


def _check_table(check: dict) -> list[str]:
    if not check.get("available"):
        return [f"cost-ledger cross-check unavailable: {check.get('reason')}"]
    rows = []
    for name, m in check["metrics"].items():
        if name == "cost_usd":
            rows.append([name, f"${m['spans']:.2f}", f"${m['ledger']:.2f}", _fmt_pct(m["diff_pct"])])
        else:
            rows.append([name, f"{m['spans']:,}", f"{m['ledger']:,}", _fmt_pct(m["diff_pct"])])
    return _table(["metric", "spans", "ledger", "diff"], rows)


def _counts_line(label: str, counts: Mapping[str, int]) -> str:
    body = ", ".join(f"{k}={v}" for k, v in counts.items()) if counts else "(none)"
    return f"{label}: {body}"


def evidence_lines(summary: dict) -> list[str]:
    if summary["empty"]:
        return ["no spans in this KST day", _counts_line("read", summary["read"])]
    totals = summary["totals"]
    lines = [
        f"spans={totals['spans']} turns={totals['turns']} tool_calls={totals['tool_calls']} "
        f"tool_error_rate={totals['tool_error_rate_pct']:.1f}%",
        _counts_line("channels", totals["by_channel"]),
        "", *_tools_table(summary), "", *_models_table(summary), "",
        *_check_table(summary["cost_ledger_check"]), "",
        _counts_line("turn status", summary["turn_outcomes"]["status"]),
        _counts_line("turn outcome", summary["turn_outcomes"]["outcome"]),
        _counts_line("turn error.type", summary["turn_outcomes"]["error_type"]),
    ]
    if summary["skills"]:
        lines.append(_counts_line("skills", summary["skills"]))
    if "a2a" in summary:
        a2a = summary["a2a"]
        lines.append(f"a2a: spans={a2a['spans']} ok={a2a['ok']} failed={a2a['failed']}")
        lines.append(_counts_line("a2a top failure classes", a2a["top_failure_classes"]))
    lines.append(_counts_line("read", summary["read"]))
    return lines


def marker_for(summary: dict) -> str:
    return f"spans-rollup:{summary['date']}:{summary['node']}"


def build_entry(summary: dict, digest_ref: Optional[str]) -> str:
    totals = summary["totals"]
    if summary["empty"]:
        headline = f"{summary['date']} 스팬 없음(KST 일 단위)."
    else:
        headline = (f"{summary['date']} 스팬 일일 롤업(KST): 턴 {totals['turns']}, "
                    f"도구 호출 {totals['tool_calls']}회, 도구 오류율 "
                    f"{totals['tool_error_rate_pct']:.1f}%. "
                    f"{_check_line(summary['cost_ledger_check'])}.")
    meta = [
        f"<!-- {marker_for(summary)} -->",
        f"## [CAND-{{id}}] {summary['date']} — {summary['node']} 스팬 일일 롤업",
        f"- suggested-path: `{SUGGESTED_PATH}`",
        "- proposed-id: assign at PR time",
        f"- class: `{ENTRY_CLASS}`",
        f"- source-session: `spans-rollup` (machine, node={summary['node']})",
        f"- distilled-at: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "- status: pending",
        f"- summary: {headline} 원천: node .telegram_bot/spans (#2684).",
    ]
    if digest_ref:
        meta.append(f"- digest-spool: `{digest_ref}`")
    meta.append("- evidence-excerpt: |")
    return "\n".join(meta + [f"    {line}" if line else "" for line in evidence_lines(summary)]) + "\n"


def build_digest_text(summary: dict) -> str:
    """Three short lines: the relay digest shows the first line + 2 details."""
    totals = summary["totals"]
    head = f"📊 스팬 롤업 {summary['date']} ({summary['node']})"
    if summary["empty"]:
        return f"{head} — 스팬 없음"
    lines = [f"{head} — 턴 {totals['turns']} · 도구 {totals['tool_calls']}회 · "
             f"도구 오류율 {totals['tool_error_rate_pct']:.1f}%",
             _check_line(summary["cost_ledger_check"])]
    top = list(summary["tools"].items())[:3]
    if top:
        lines.append("상위 도구: " + " · ".join(
            f"{name} {t['calls']} (p95 {_fmt_ms(t['p95_ms'])}ms)" for name, t in top))
    return "\n".join(lines)


# --- delivery -----------------------------------------------------------------


def _state_dir() -> Path:
    claude_dir = os.environ.get("CCC_CLAUDE_DIR") or str(Path.home() / ".claude")
    return Path(os.environ.get("CCC_STATE_DIR") or Path(claude_dir) / "state").expanduser()


def push_spool_dir() -> Path:
    """Same resolution order as the other spool writers (agent-cron, timed scan)."""
    raw = os.environ.get("CCC_PUSH_SPOOL")
    return Path(raw).expanduser() if raw else _state_dir() / "telegram-spool"


def write_digest_spool(summary: dict, now: datetime) -> Path:
    """One owner-only ``tier=info`` record (0600, atomic rename) for the daily digest."""
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    import ccc_secure_fs

    spool = push_spool_dir()
    spool.mkdir(parents=True, exist_ok=True)
    stamp = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = {
        "ts": stamp,
        "event": NOTIFY_EVENT,
        "node": summary["node"],
        "text": build_digest_text(summary),
        "dedup": f"{NOTIFY_EVENT}:{summary['date']}:{summary['node']}",
        "tier": "info",
    }
    name = f"{stamp.replace(':', '-')}-{NOTIFY_EVENT}-{os.getpid()}-{secrets.token_hex(4)}.json"
    path = spool / name
    ccc_secure_fs.atomic_write_text(
        path, json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", mode=0o600)
    return path


def _next_cand_id(queue_text: str) -> int:
    weekly = SCRIPT_DIR / "cost-ledger-weekly.py"
    spec = importlib.util.spec_from_file_location("_ccc_cost_ledger_weekly", weekly)
    if spec is None or spec.loader is None:
        raise RuntimeError("cost-ledger-weekly.py unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return int(module.next_cand_id(queue_text))


QUEUE_HEADER = (
    "# Wiki Candidates Queue (auto-generated by distill; review with `/wiki-record`)\n\n"
    "> Each entry is a durable operational fact / decision proposed by the Session\n"
    "> Distiller (TM-1058). Review and either:\n"
    ">   - Promote via `/wiki-record` (creates PR), then mark status: merged below.\n"
    ">   - Reject by deleting the entry (or marking status: rejected).\n>\n"
    "> Never auto-PR — this is a human-gated queue.\n\n"
)


def append_queue_entry(queue: Path, entry: str) -> int:
    queue.parent.mkdir(parents=True, exist_ok=True)
    if not queue.is_file():
        queue.write_text(QUEUE_HEADER, encoding="utf-8")
    queue_text = queue.read_text(encoding="utf-8")
    cand_id = _next_cand_id(queue_text)
    with queue.open("a", encoding="utf-8") as handle:
        if queue_text and not queue_text.endswith("\n"):
            handle.write("\n")
        handle.write("\n" + entry.replace("{id}", str(cand_id)))
    return cand_id


# --- CLI ----------------------------------------------------------------------


def _parse_day(value: Optional[str], now: datetime) -> date:
    if value:
        return datetime.strptime(value, "%Y-%m-%d").date()
    return (now.astimezone(KST) - timedelta(days=1)).date()


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--date", help="KST day YYYY-MM-DD to roll up (default: yesterday KST)")
    ap.add_argument("--project-root", type=Path,
                    default=Path(os.environ.get("PROJECT_ROOT") or Path.home()),
                    help="bridge PROJECT_ROOT (default: $PROJECT_ROOT, else $HOME)")
    ap.add_argument("--spans-dir", type=Path,
                    help="span ledger dir (default: <project-root>/.telegram_bot/spans)")
    ap.add_argument("--cost-ledger", type=Path,
                    help="bridge cost ledger (default: "
                         "<project-root>/.telegram_bot/usage-cost-ledger.jsonl)")
    ap.add_argument("--queue", type=Path, default=_state_dir() / "wiki-candidates.md")
    ap.add_argument("--node", default=None)
    ap.add_argument("--no-digest", action="store_true",
                    help="queue the wiki candidate only; do not write the push-spool record")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the entry and digest text; deliver nothing")
    ap.add_argument("--json", action="store_true", help="with --dry-run: print the summary JSON")
    return ap


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    now = datetime.now(timezone.utc)
    try:
        day = _parse_day(args.date, now)
    except ValueError:
        print(json.dumps({"ok": False, "error": "bad --date (want YYYY-MM-DD)"}))
        return 2
    node = args.node or resolve_node()
    data_dir = args.project_root.expanduser() / ".telegram_bot"
    spans_dir = (args.spans_dir or data_dir / "spans").expanduser()
    ledger_path = (args.cost_ledger or data_dir / "usage-cost-ledger.jsonl").expanduser()

    marker = f"spans-rollup:{day.isoformat()}:{node}"
    queue_text = args.queue.read_text(encoding="utf-8") if args.queue.is_file() else ""
    if not args.dry_run and marker in queue_text:
        print(json.dumps({"ok": True, "skipped": "already-queued", "date": day.isoformat(),
                          "node": node}))
        return 0

    try:
        sanitize = load_span_ledger().sanitize_span
    except Exception as exc:  # fail closed: never aggregate unsanitized spans
        print(json.dumps({"ok": False, "error": f"span sanitizer unavailable: {type(exc).__name__}"}))
        return 4

    start, end = kst_day_window(day)
    spans, stats = read_spans(utc_files_for_window(spans_dir, start, end), start, end, sanitize)
    summary = build_summary(day, node, spans, stats, read_cost_ledger(ledger_path, start, end))

    if args.dry_run:
        if args.json:
            print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(build_entry(summary, None).replace("{id}", "?"))
            print("--- digest (not sent) ---")
            print(build_digest_text(summary))
        return 0

    if summary["empty"]:
        print(json.dumps({"ok": True, "skipped": "no-spans", "date": day.isoformat(),
                          "node": node, "read": stats}))
        return 0

    digest_ref = None
    if not args.no_digest:
        # Spool first: its file name is the delivery evidence recorded in the
        # entry. If the queue append then fails, the next run re-spools (the
        # relay's dedup window absorbs a quick retry) instead of skipping the
        # digest forever behind an already-written marker.
        digest_ref = write_digest_spool(summary, now).name
    cand_id = append_queue_entry(args.queue, build_entry(summary, digest_ref))
    print(json.dumps({"ok": True, "queued": day.isoformat(), "node": node, "cand": cand_id,
                      "spans": summary["totals"]["spans"], "digest_spool": digest_ref}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
