"""Hermetic tests for scripts/spans-rollup.py (#2684 slice 3).

Fixtures are synthetic span / cost-ledger lines in a temp project root, so
every assertion is about the rollup's arithmetic, KST window, body-free
contract and delivery — never about this host's real ledger.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "spans-rollup.py"
_SPEC = importlib.util.spec_from_file_location("spans_rollup", SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
rollup = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(rollup)

DAY = "2026-11-06"  # KST day == 2026-11-05T15:00Z .. 2026-11-06T15:00Z
IN_DAY = datetime(2026, 11, 6, 1, 0, tzinfo=timezone.utc).timestamp()  # 10:00 KST
EARLY_IN_DAY = datetime(2026, 11, 5, 15, 30, tzinfo=timezone.utc).timestamp()  # 00:30 KST
BEFORE_DAY = datetime(2026, 11, 5, 14, 59, tzinfo=timezone.utc).timestamp()  # 23:59 KST prev
AFTER_DAY = datetime(2026, 11, 6, 15, 0, tzinfo=timezone.utc).timestamp()  # 00:00 KST next
BODY = "SECRET BODY TEXT please never copy me"


def tool(name, ts, ms, status="ok", **extra):
    span = {"kind": "tool", "status": status, "start_ts": ts, "duration_ms": ms,
            "gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": name,
            "ccc.tool.scope": "main", "ccc.node": "testnode", "ccc.channel": "telegram"}
    if status != "ok":
        span["error.type"] = "tool_error"
    span.update(extra)
    return span


def turn(ts, model="claude-opus-5-5", inp=1100, out=200, cache_read=1000, cache_create=0,
         cost=0.5, status="ok", outcome="success", **extra):
    span = {"kind": "turn", "status": status, "start_ts": ts, "duration_ms": 9000,
            "gen_ai.operation.name": "invoke_agent", "gen_ai.request.model": model,
            "gen_ai.usage.input_tokens": inp, "gen_ai.usage.output_tokens": out,
            "ccc.usage.cache_read_tokens": cache_read,
            "ccc.usage.cache_creation_tokens": cache_create, "ccc.cost_usd": cost,
            "ccc.outcome": outcome, "ccc.node": "testnode", "ccc.channel": "telegram"}
    span.update(extra)
    return span


class Env:
    def __init__(self, tmp_path: Path):
        self.root = tmp_path / "project"
        self.spans = self.root / ".telegram_bot" / "spans"
        self.ledger = self.root / ".telegram_bot" / "usage-cost-ledger.jsonl"
        self.state = tmp_path / "state"
        self.queue = self.state / "wiki-candidates.md"
        self.spool = self.state / "telegram-spool"

    def write_spans(self, utc_day: str, spans, raw_lines=()):
        self.spans.mkdir(parents=True, exist_ok=True)
        with (self.spans / f"{utc_day}.jsonl").open("a", encoding="utf-8") as fh:
            for span in spans:
                fh.write(json.dumps(span) + "\n")
            for line in raw_lines:
                fh.write(line)

    def ledger_row(self, ts, cost, models):
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        with self.ledger.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": ts, "provider": "claude", "total_cost_usd": cost,
                                 "models": models, "sid": "raw-session-id"}) + "\n")

    def run(self, *args, monkeypatch=None):
        argv = ["--date", DAY, "--project-root", str(self.root), "--queue", str(self.queue),
                "--node", "testnode", *args]
        return rollup.main(argv)

    def spool_files(self):
        return sorted(self.spool.glob("*.json")) if self.spool.is_dir() else []


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = Env(tmp_path)
    monkeypatch.setenv("CCC_STATE_DIR", str(e.state))
    monkeypatch.delenv("CCC_PUSH_SPOOL", raising=False)
    return e


def summarize(env: Env):
    start, end = rollup.kst_day_window(date.fromisoformat(DAY))
    sanitize = rollup.load_span_ledger().sanitize_span
    spans, stats = rollup.read_spans(
        rollup.utc_files_for_window(env.spans, start, end), start, end, sanitize)
    ledger = rollup.read_cost_ledger(env.ledger, start, end)
    return rollup.build_summary(date.fromisoformat(DAY), "testnode", spans, stats, ledger)


# --- math ---------------------------------------------------------------------


def test_percentile_linear_interpolation():
    values = list(range(1, 11))
    assert rollup.percentile(values, 50) == 5.5
    assert rollup.percentile(values, 95) == pytest.approx(9.55)
    assert rollup.percentile([42], 95) == 42.0
    assert rollup.percentile([], 50) is None
    assert rollup.percentile([30, 10, 20], 50) == 20.0  # unsorted input


def test_tool_error_rate_and_latency(env):
    env.write_spans("2026-11-06", [
        tool("Bash", IN_DAY, 100), tool("Bash", IN_DAY + 1, 200),
        tool("Bash", IN_DAY + 2, 300), tool("Bash", IN_DAY + 3, 400, status="error"),
        tool("Read", IN_DAY + 4, 5),
    ])
    summary = summarize(env)
    bash = summary["tools"]["Bash"]
    assert bash == {"calls": 4, "errors": 1, "error_rate_pct": 25.0,
                    "p50_ms": 250.0, "p95_ms": 385.0}
    assert list(summary["tools"]) == ["Bash", "Read"]  # most calls first
    assert summary["totals"]["tool_error_rate_pct"] == 20.0


# --- reading rules --------------------------------------------------------------


def test_corrupt_and_foreign_lines_are_skipped_and_counted(env):
    env.write_spans("2026-11-06", [tool("Bash", IN_DAY, 10)], raw_lines=[
        "{not json\n", "\n", "[1, 2]\n", json.dumps({"kind": "bogus", "status": "ok"}) + "\n",
        '{"kind":"tool","status":"ok","start_ts":',  # torn trailing line, no newline
    ])
    summary = summarize(env)
    assert summary["totals"]["tool_calls"] == 1
    assert summary["read"]["corrupt_lines"] == 2  # "{not json" + torn tail
    assert summary["read"]["rejected_spans"] == 2  # list + unknown kind


def test_kst_window_spans_two_utc_files(env):
    env.write_spans("2026-11-05", [tool("Early", EARLY_IN_DAY, 1), tool("Before", BEFORE_DAY, 1)])
    env.write_spans("2026-11-06", [tool("Late", IN_DAY, 1), tool("After", AFTER_DAY, 1)])
    summary = summarize(env)
    assert sorted(summary["tools"]) == ["Early", "Late"]
    assert summary["read"]["outside_window"] == 2
    assert summary["read"]["files_read"] == 2


def test_missing_dir_and_empty_day_exit_zero_without_delivery(env, capsys):
    assert env.run() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["skipped"] == "no-spans" and out["ok"] is True
    assert not env.queue.exists() and env.spool_files() == []
    env.write_spans("2026-11-06", [], raw_lines=["garbage\n"])
    assert env.run("--dry-run") == 0
    assert "no spans in this KST day" in capsys.readouterr().out


# --- cost ledger cross-check ----------------------------------------------------


def test_ledger_cross_check_matches_and_reports_discrepancy(env):
    env.write_spans("2026-11-06", [
        turn(IN_DAY, inp=1100, out=200, cache_read=1000, cost=0.5),
        turn(IN_DAY + 60, inp=2050, out=100, cache_read=0, cache_create=50, cost=0.25),
        turn(IN_DAY + 120, cost=0.0, out=999),  # unpriced turn: the ledger writes no row
    ])
    env.ledger_row(IN_DAY + 9, 0.5, [{"model": "claude-opus-5-5", "cost_usd": 0.5,
                                      "in": 100, "out": 200}])
    env.ledger_row(IN_DAY + 69, 0.25, [{"model": "claude-opus-5-5", "cost_usd": 0.2, "in": 1900,
                                        "out": 90},
                                       {"model": "claude-haiku-5-5", "cost_usd": 0.05, "in": 100,
                                        "out": 20}])
    env.ledger_row(BEFORE_DAY, 9.0, [{"model": "x", "cost_usd": 9.0, "in": 9, "out": 9}])
    with env.ledger.open("a") as fh:
        fh.write("{torn\n")
        fh.write("[1, 2]\n")  # valid JSON, not a row
        # Malformed shapes inside the window must not crash the rollup.
        fh.write(json.dumps({"ts": IN_DAY + 99, "total_cost_usd": "x", "models": 5}) + "\n")
    check = summarize(env)["cost_ledger_check"]
    assert check["available"] is True
    m = check["metrics"]
    assert m["turns"] == {"spans": 2, "ledger": 2, "diff_pct": 0.0}
    assert m["input_tokens"] == {"spans": 2100, "ledger": 2100, "diff_pct": 0.0}
    assert m["output_tokens"] == {"spans": 300, "ledger": 310, "diff_pct": 3.23}
    assert m["cost_usd"]["diff_pct"] == 0.0
    assert check["max_diff_pct"] == 3.23 and check["within_target"] is True
    assert check["ledger_corrupt_lines"] == 3


def test_ledger_cross_check_flags_over_target_and_missing_ledger(env):
    env.write_spans("2026-11-06", [turn(IN_DAY, out=200, cost=0.5)])
    assert summarize(env)["cost_ledger_check"] == {
        "available": False, "reason": "cost ledger missing or unreadable"}
    env.ledger_row(IN_DAY, 0.5, [{"model": "m", "cost_usd": 0.5, "in": 100, "out": 100}])
    check = summarize(env)["cost_ledger_check"]
    assert check["metrics"]["output_tokens"]["diff_pct"] == 100.0
    assert check["within_target"] is False
    assert "⚠" in rollup.build_digest_text(summarize(env))


def test_ledger_cross_check_buckets_turns_by_end_time_across_midnight(env, capsys):
    # Live case (two fleet nodes, 2026-11-16): a turn that starts 23:59 and ends
    # 00:01 is a start-day span but a next-day ledger row (ts = turn end).
    late_in_day = AFTER_DAY - 60  # 23:59 KST of DAY
    env.write_spans("2026-11-05", [turn(BEFORE_DAY, out=300, cost=0.3, duration_ms=120_000)])
    env.write_spans("2026-11-06", [
        turn(IN_DAY, out=200, cost=0.2),
        turn(late_in_day, out=700, cost=0.7, duration_ms=120_000),
    ])
    env.ledger_row(BEFORE_DAY + 120, 0.3, [{"model": "m", "cost_usd": 0.3, "in": 100, "out": 300}])
    env.ledger_row(IN_DAY + 9, 0.2, [{"model": "m", "cost_usd": 0.2, "in": 100, "out": 200}])
    env.ledger_row(late_in_day + 120, 0.7, [{"model": "m", "cost_usd": 0.7, "in": 100, "out": 700}])

    # Start-bucketed comparison (the old behaviour) drifts by the crossing turns.
    legacy = summarize(env)["cost_ledger_check"]
    assert legacy["turn_basis"] == "start"
    assert legacy["metrics"]["output_tokens"] == {"spans": 900, "ledger": 500, "diff_pct": 80.0}
    assert legacy["within_target"] is False

    assert env.run("--dry-run", "--json") == 0
    summary = json.loads(capsys.readouterr().out)
    check = summary["cost_ledger_check"]
    assert check["turn_basis"] == "end"
    assert check["metrics"]["turns"] == {"spans": 2, "ledger": 2, "diff_pct": 0.0}
    assert check["metrics"]["output_tokens"] == {"spans": 500, "ledger": 500, "diff_pct": 0.0}
    assert check["max_diff_pct"] == 0.0 and check["within_target"] is True
    # The day rollup itself stays start-bucketed: the 23:59 turn belongs to DAY.
    assert summary["totals"]["turns"] == 2
    assert summary["read"]["outside_window"] == 1  # the previous-day 23:59 turn
    assert summary["read"]["files_read"] == 2


def test_ledger_cross_check_leaves_out_zero_cost_ledger_rows(env, capsys):
    # Live case (one fleet node, 2026-11-18): a turn that ended in
    # error_during_execution added no spend. The per-turn delta still listed
    # the session's two models at cost 0, so the ledger wrote a
    # total_cost_usd 0.0 row for it. Counting that row on the ledger side only
    # read 3 turns vs 2 priced spans (33.33 %) with cost and tokens identical.
    zero_models = [{"model": "m", "cost_usd": 0.0, "in": 0, "out": 0},
                   {"model": "aux", "cost_usd": 0.0, "in": 0, "out": 0}]
    env.write_spans("2026-11-06", [
        turn(IN_DAY, inp=0, out=0, cache_read=0, cost=0.0, status="error",
             outcome="error_during_execution", **{"error.type": "error_during_execution"}),
        turn(IN_DAY + 90, inp=2, out=4026, cache_read=0, cost=0.131537),
        turn(IN_DAY + 110, inp=2, out=361, cache_read=0, cost=0.088644),
    ])
    env.ledger_row(IN_DAY + 9, 0.0, zero_models)
    env.ledger_row(IN_DAY + 99, 0.131537, [{"model": "m", "cost_usd": 0.131537, "in": 2,
                                            "out": 4026}])
    env.ledger_row(IN_DAY + 119, 0.088644, [{"model": "m", "cost_usd": 0.088644, "in": 2,
                                             "out": 361}])

    assert env.run("--dry-run", "--json") == 0
    summary = json.loads(capsys.readouterr().out)
    check = summary["cost_ledger_check"]
    assert check["metrics"]["turns"] == {"spans": 2, "ledger": 2, "diff_pct": 0.0}
    assert check["metrics"]["cost_usd"] == {"spans": 0.220181, "ledger": 0.220181, "diff_pct": 0.0}
    assert check["max_diff_pct"] == 0.0 and check["within_target"] is True
    assert check["ledger_zero_cost_rows"] == 1
    # The error turn is still a turn of the day: only the cross-check skips it.
    assert summary["totals"]["turns"] == 3
    assert "⚠" not in rollup.build_digest_text(summary)
    assert ("ledger zero-cost rows left out of the comparison: 1"
            in "\n".join(rollup._check_table(check)))


def test_zero_cost_ledger_rows_add_nothing_to_any_ledger_total(env):
    # A zero-cost row that still carries tokens, and a row whose total is null
    # while its models are priced: the span side keys on the turn total
    # (``ccc.cost_usd``), so the ledger side keys on ``total_cost_usd`` alone.
    start, end = rollup.kst_day_window(date.fromisoformat(DAY))
    env.ledger_row(IN_DAY, 0.5, [{"model": "m", "cost_usd": 0.5, "in": 100, "out": 200}])
    env.ledger_row(IN_DAY + 1, 0.0, [{"model": "m", "cost_usd": 0.0, "in": 70, "out": 30}])
    env.ledger_row(IN_DAY + 2, None, [{"model": "m", "cost_usd": 0.4, "in": 7, "out": 3}])
    assert rollup.read_cost_ledger(env.ledger, start, end) == {
        "turns": 1, "input_tokens": 100, "output_tokens": 200, "cost_usd": 0.5,
        "corrupt_lines": 0, "zero_cost_rows": 2}
    # No zero-cost rows: nothing extra is rendered.
    env.ledger.unlink()
    env.ledger_row(IN_DAY, 0.5, [{"model": "m", "cost_usd": 0.5, "in": 100, "out": 200}])
    env.write_spans("2026-11-06", [turn(IN_DAY, inp=100, out=200, cache_read=0, cost=0.5)])
    check = summarize(env)["cost_ledger_check"]
    assert check["ledger_zero_cost_rows"] == 0
    assert not any("zero-cost" in line for line in rollup._check_table(check))


def test_turn_end_ts_tolerates_missing_or_bad_duration():
    assert rollup.turn_end_ts({"start_ts": 100.0, "duration_ms": 2500}) == 102.5
    assert rollup.turn_end_ts({"start_ts": 100.0}) == 100.0
    assert rollup.turn_end_ts({"start_ts": 100.0, "duration_ms": -5}) == 100.0
    assert rollup.turn_end_ts({"start_ts": 100.0, "duration_ms": True}) == 100.0
    assert rollup.turn_end_ts({"start_ts": "x"}) is None


def test_models_skills_outcomes_and_a2a(env):
    env.write_spans("2026-11-06", [
        turn(IN_DAY, model="m-a", **{"ccc.skill": "gh-pr-flow"}),
        turn(IN_DAY + 1, model="m-a", status="error", outcome="error_max_turns",
             **{"error.type": "error_max_turns"}),
        turn(IN_DAY + 2, model="m-b"),
    ])
    summary = summarize(env)
    assert summary["models"]["m-a"]["turns"] == 2 and summary["models"]["m-b"]["turns"] == 1
    assert summary["skills"] == {"gh-pr-flow": 1}
    assert summary["turn_outcomes"]["status"] == {"ok": 2, "error": 1}
    assert summary["turn_outcomes"]["error_type"] == {"error_max_turns": 1}
    assert "a2a" not in summary  # omitted when no a2a spans exist
    a2a = [{"kind": "a2a", "status": "error", "start_ts": IN_DAY + i,
            "error.type": f"class{i % 7}"} for i in range(20)]
    a2a.append({"kind": "a2a", "status": "ok", "start_ts": IN_DAY + 30})
    env.write_spans("2026-11-06", a2a)
    block = summarize(env)["a2a"]
    assert block["spans"] == 21 and block["ok"] == 1 and block["failed"] == 20
    assert len(block["top_failure_classes"]) == 5
    assert list(block["top_failure_classes"].values())[0] == 3


# --- a2a worker contract (#2858) -------------------------------------------------

#: Lines exactly as the a2a-nexus worker writes them
#: (packages/broker/src/workers/a2a-span-writer.ts, ``A2ASpanWriter.record``):
#: two tasks of claim → dispatch → receipt plus a timed-out dispatch whose
#: completion report the broker rejected. Payload/result appear only as
#: byte counts and sha256 prefixes.
A2A_WORKER_LINES = [
    '{"kind":"a2a","status":"ok","gen_ai.operation.name":"a2a.claim","start_ts":1793926800,"duration_ms":120,"ccc.node":"testnode","ccc.outcome":"claimed","ccc.a2a.task_ref":"cb45de7833575506","ccc.a2a.payload_bytes":43,"ccc.a2a.payload_sha256":"4402da1899f48e37"}',
    '{"kind":"a2a","status":"ok","gen_ai.operation.name":"a2a.dispatch","start_ts":1793926800.2,"duration_ms":90000,"ccc.node":"testnode","ccc.outcome":"completed","ccc.a2a.task_ref":"cb45de7833575506","ccc.a2a.result_bytes":44,"ccc.a2a.result_sha256":"b7824438b438268e"}',
    '{"kind":"a2a","status":"ok","gen_ai.operation.name":"a2a.receipt","start_ts":1793926890.3,"duration_ms":100,"ccc.node":"testnode","ccc.outcome":"complete","ccc.a2a.task_ref":"cb45de7833575506"}',
    '{"kind":"a2a","status":"ok","gen_ai.operation.name":"a2a.claim","start_ts":1793926801,"duration_ms":100,"ccc.node":"testnode","ccc.outcome":"claimed","ccc.a2a.task_ref":"28905e9ccc89e153","ccc.a2a.payload_bytes":2,"ccc.a2a.payload_sha256":"44136fa355b3678a"}',
    '{"kind":"a2a","status":"error","gen_ai.operation.name":"a2a.dispatch","start_ts":1793926801.2,"duration_ms":4000,"ccc.node":"testnode","error.type":"handler_exit_nonzero","ccc.outcome":"failed","ccc.a2a.task_ref":"28905e9ccc89e153"}',
    '{"kind":"a2a","status":"ok","gen_ai.operation.name":"a2a.receipt","start_ts":1793926805.3,"duration_ms":100,"ccc.node":"testnode","ccc.outcome":"fail","ccc.a2a.task_ref":"28905e9ccc89e153"}',
    '{"kind":"a2a","status":"timeout","gen_ai.operation.name":"a2a.dispatch","start_ts":1793926806,"duration_ms":3000,"ccc.node":"testnode","error.type":"handler_timeout","ccc.outcome":"failed","ccc.a2a.task_ref":"f0fdb386c77310f0"}',
    '{"kind":"a2a","status":"error","gen_ai.operation.name":"a2a.receipt","start_ts":1793926809.1,"duration_ms":100,"ccc.node":"testnode","error.type":"receipt_http_500","ccc.outcome":"complete","ccc.a2a.task_ref":"f0fdb386c77310f0"}',
]


def test_a2a_worker_lines_survive_sanitize_unchanged():
    sanitize = rollup.load_span_ledger().sanitize_span
    for line in A2A_WORKER_LINES:
        span = json.loads(line)
        assert sanitize(span) == span, line


def test_a2a_worker_lines_roll_up_into_real_a2a_values(env):
    assert IN_DAY == 1793926800  # the fixture lines start at 10:00 KST on DAY
    env.write_spans("2026-11-06", [turn(IN_DAY)], raw_lines=[line + "\n" for line in A2A_WORKER_LINES])
    summary = summarize(env)
    assert summary["a2a"] == {
        "spans": 8, "ok": 5, "failed": 3,
        "top_failure_classes": {"handler_exit_nonzero": 1, "handler_timeout": 1, "receipt_http_500": 1},
    }
    lines = rollup.evidence_lines(summary)
    assert "a2a: spans=8 ok=5 failed=3" in lines
    assert ("a2a top failure classes: handler_exit_nonzero=1, handler_timeout=1, "
            "receipt_http_500=1") in lines
    # The owner's 3-line digest carries the a2a counts at the end of its first line (#2858, owner "1").
    digest = rollup.build_digest_text(summary).split("\n")
    assert digest[0].endswith(" · a2a 8(실패 3)")
    assert len(digest) <= 3


def test_digest_has_no_a2a_suffix_without_a2a_spans(env):
    env.write_spans("2026-11-06", [tool("Bash", IN_DAY, 10), turn(IN_DAY + 1)])
    assert "a2a" not in rollup.build_digest_text(summarize(env))


def test_digest_a2a_only_day_keeps_head_line(env):
    env.write_spans("2026-11-06", [], raw_lines=[line + "\n" for line in A2A_WORKER_LINES[:1]])
    summary = summarize(env)
    assert not summary["empty"] and summary["a2a"]["spans"] == 1
    head = rollup.build_digest_text(summary).split("\n")[0]
    assert head.startswith("📊 스팬 롤업 2026-11-06 (testnode) — 턴 0")
    assert head.endswith(f" · a2a 1(실패 {summary['a2a']['failed']})")


def test_a2a_body_text_never_reaches_rollup_output(env, capsys):
    hostile = {"kind": "a2a", "status": "error", "start_ts": IN_DAY + 5,
               "gen_ai.operation.name": "a2a.dispatch", "error.type": BODY,
               "ccc.outcome": BODY, "ccc.a2a.payload_sha256": BODY,
               "task.payload": BODY, "error.message": BODY, "gen_ai.prompt": BODY}
    env.write_spans("2026-11-06", [turn(IN_DAY), hostile])
    assert env.run("--dry-run", "--json") == 0
    out = capsys.readouterr().out
    assert BODY not in out
    assert json.loads(out)["a2a"] == {"spans": 1, "ok": 0, "failed": 1,
                                      "top_failure_classes": {"error": 1}}


# --- body-free contract ---------------------------------------------------------


def test_body_free_output_drops_unexpected_text(env, capsys):
    env.write_spans("2026-11-06", [
        tool("Bash", IN_DAY, 10, prompt=BODY, **{"tool.input": BODY, "gen_ai.prompt": BODY}),
        tool(BODY, IN_DAY + 1, 10),  # whitespace: not identifier-shaped, name dropped
        turn(IN_DAY + 2, **{"ccc.outcome": BODY, "answer": BODY}),
    ])
    env.ledger_row(IN_DAY, 0.5, [{"model": BODY, "cost_usd": 0.5, "in": 100, "out": 200}])
    summary = summarize(env)
    rendered = [json.dumps(summary, ensure_ascii=False), rollup.build_entry(summary, "x.json"),
                rollup.build_digest_text(summary)]
    for text in rendered:
        assert "SECRET" not in text and "never copy" not in text
        assert "raw-session-id" not in text
    assert set(summary["tools"]) == {"Bash", rollup.UNNAMED}
    assert env.run("--dry-run", "--json") == 0
    assert "SECRET" not in capsys.readouterr().out


# --- delivery -------------------------------------------------------------------


def test_dry_run_delivers_nothing(env, capsys):
    env.write_spans("2026-11-06", [tool("Bash", IN_DAY, 10), turn(IN_DAY + 1)])
    assert env.run("--dry-run") == 0
    out = capsys.readouterr().out
    assert "<!-- spans-rollup:2026-11-06:testnode -->" in out and "digest (not sent)" in out
    assert not env.queue.exists()
    assert env.spool_files() == []


def test_write_run_queues_entry_and_spools_one_info_digest(env, capsys):
    env.write_spans("2026-11-06", [tool("Bash", IN_DAY, 10), turn(IN_DAY + 1)])
    env.state.mkdir(parents=True)
    env.queue.write_text("# q\n\n## [CAND-4] earlier\n- status: merged\n", encoding="utf-8")
    assert env.run() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] and result["cand"] == 5
    files = env.spool_files()
    assert len(files) == 1 and result["digest_spool"] == files[0].name
    assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
    record = json.loads(files[0].read_text(encoding="utf-8"))
    assert record["tier"] == "info" and record["event"] == "SpansRollup"
    assert record["node"] == "testnode" and "chatId" not in record
    assert record["dedup"] == "SpansRollup:2026-11-06:testnode"
    assert record["text"].startswith("📊 스팬 롤업 2026-11-06 (testnode)")
    queue = env.queue.read_text(encoding="utf-8")
    assert queue.startswith("# q\n")  # existing content preserved
    assert "## [CAND-5] 2026-11-06 — testnode 스팬 일일 롤업" in queue
    assert "- class: `metrics/span-rollup`" in queue and "- status: pending" in queue
    assert f"- digest-spool: `{files[0].name}`" in queue
    # Idempotent: the marker stops a second delivery.
    assert env.run() == 0
    assert json.loads(capsys.readouterr().out)["skipped"] == "already-queued"
    assert len(env.spool_files()) == 1 and queue.count("spans-rollup:2026-11-06:testnode") == 1


def test_no_digest_queues_only_and_bootstraps_queue(env, capsys):
    env.write_spans("2026-11-06", [tool("Bash", IN_DAY, 10)])
    assert env.run("--no-digest") == 0
    assert json.loads(capsys.readouterr().out)["digest_spool"] is None
    assert env.spool_files() == []
    text = env.queue.read_text(encoding="utf-8")
    assert text.startswith("# Wiki Candidates Queue") and "## [CAND-1]" in text


def test_default_date_is_yesterday_kst():
    now = datetime(2026, 11, 6, 15, 30, tzinfo=timezone.utc)  # 00:30 KST 11-07
    assert rollup._parse_day(None, now) == date(2026, 11, 6)
    assert rollup._parse_day("2026-01-02", now) == date(2026, 1, 2)


def test_bad_date_exits_2(env, capsys):
    assert rollup.main(["--date", "2026-13-40", "--queue", str(env.queue)]) == 2
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_isolated_interpreter_uses_checkout_sanitizer(env):
    """`python3 -I` cannot import telegram_bot: the path-loaded shim must work and be body-free."""
    env.write_spans("2026-11-06", [tool("Bash", IN_DAY, 10, prompt=BODY), turn(IN_DAY + 1)])
    proc = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "--date", DAY, "--project-root", str(env.root),
         "--queue", str(env.queue), "--node", "testnode", "--dry-run", "--json"],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "CCC_STATE_DIR": str(env.state)})
    assert proc.returncode == 0, proc.stderr
    summary = json.loads(proc.stdout)
    assert summary["tools"]["Bash"]["calls"] == 1 and "SECRET" not in proc.stdout


# --- tick receipt for doctor's `spans-rollup tick` row ---------------------------


def read_status(env: Env) -> dict:
    path = env.state / rollup.STATUS_FILE
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data) == {"schema", "finished_at", "ok", "result_code", "date", "node"}
    assert data["schema"] == rollup.STATUS_SCHEMA and data["date"] == DAY
    assert data["node"] == "testnode"
    datetime.strptime(data["finished_at"], "%Y-%m-%dT%H:%M:%SZ")
    return data


def test_status_file_written_for_queued_and_already_queued(env, capsys):
    env.write_spans("2026-11-06", [tool("Bash", IN_DAY, 10, prompt=BODY), turn(IN_DAY + 1)])
    assert env.run() == 0
    status = read_status(env)
    assert status["ok"] is True and status["result_code"] == "queued"
    assert "SECRET" not in (env.state / rollup.STATUS_FILE).read_text(encoding="utf-8")
    assert env.run() == 0
    assert read_status(env)["result_code"] == "already-queued"
    assert not list(env.state.glob(f".{rollup.STATUS_FILE}.*"))  # no temp leftovers


def test_status_file_written_for_no_spans(env, capsys):
    assert env.run() == 0
    status = read_status(env)
    assert status["ok"] is True and status["result_code"] == "no-spans"


def test_status_file_never_written_on_dry_run(env, capsys):
    env.write_spans("2026-11-06", [tool("Bash", IN_DAY, 10)])
    assert env.run("--dry-run") == 0
    assert env.run("--dry-run", "--json") == 0
    assert not (env.state / rollup.STATUS_FILE).exists()


def test_status_file_records_sanitizer_failure(env, capsys, monkeypatch):
    def boom():
        raise ImportError("gone")

    monkeypatch.setattr(rollup, "load_span_ledger", boom)
    assert env.run("--dry-run") == 4
    assert not (env.state / rollup.STATUS_FILE).exists()
    assert env.run() == 4
    status = read_status(env)
    assert status["ok"] is False and status["result_code"] == "sanitizer-unavailable"


def test_status_file_records_crash_and_reraises(env, capsys, monkeypatch):
    env.write_spans("2026-11-06", [tool("Bash", IN_DAY, 10)])

    def broken_append(queue, entry):
        raise OSError("disk full")

    monkeypatch.setattr(rollup, "append_queue_entry", broken_append)
    with pytest.raises(OSError):
        env.run("--no-digest")
    status = read_status(env)
    assert status["ok"] is False and status["result_code"] == "crashed"


def test_status_write_failure_keeps_exit_code(env, capsys, monkeypatch):
    env.state.parent.mkdir(parents=True, exist_ok=True)
    blocker = env.state.parent / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("CCC_STATE_DIR", str(blocker))
    assert env.run() == 0  # no-spans day; the receipt cannot be written
    captured = capsys.readouterr()
    assert json.loads(captured.out)["skipped"] == "no-spans"
    assert "status file not written" in captured.err


# --- per-day summary file for the read-only view (#2858 item 4, piece 1) ----------


def summary_path(env: Env, day: str = DAY) -> Path:
    return env.state / rollup.SUMMARY_DIR / f"{day}.json"


def test_summary_file_matches_dry_run_json_and_is_owner_only(env, capsys):
    env.write_spans("2026-11-06", [tool("Bash", IN_DAY, 10, prompt=BODY), turn(IN_DAY + 1)])
    assert env.run("--dry-run", "--json") == 0
    expected = json.loads(capsys.readouterr().out)
    assert not summary_path(env).exists()  # dry-run writes nothing
    assert env.run() == 0
    capsys.readouterr()
    path = summary_path(env)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    text = path.read_text(encoding="utf-8")
    assert "SECRET" not in text
    assert json.loads(text) == expected
    assert not list(path.parent.glob(".*.tmp"))  # no temp leftovers


def test_summary_file_written_for_no_spans_day(env, capsys):
    assert env.run() == 0
    data = json.loads(summary_path(env).read_text(encoding="utf-8"))
    assert data["empty"] is True and data["date"] == DAY and data["node"] == "testnode"


def test_summary_file_prunes_only_expired_day_files(env, capsys):
    folder = env.state / rollup.SUMMARY_DIR
    folder.mkdir(parents=True)
    expired = folder / "2026-08-07.json"  # 91 days before 2026-11-06
    kept = folder / "2026-08-08.json"  # exactly SUMMARY_KEEP_DAYS old
    other = folder / "notes.txt"
    outside = env.state / "outside.json"
    for p in (expired, kept, other, outside):
        p.write_text("{}", encoding="utf-8")
    link = folder / "2026-01-01.json"
    link.symlink_to(outside)
    assert rollup.SUMMARY_KEEP_DAYS == 90
    assert env.run() == 0
    assert not expired.exists()
    assert kept.exists() and other.exists() and outside.exists()
    assert link.is_symlink()  # never followed or removed


def test_summary_file_refuses_symlinked_dir_and_keeps_exit_code(env, capsys):
    target = env.state.parent / "elsewhere"
    target.mkdir(parents=True)
    env.state.mkdir(parents=True)
    (env.state / rollup.SUMMARY_DIR).symlink_to(target)
    env.write_spans("2026-11-06", [tool("Bash", IN_DAY, 10)])
    assert env.run("--no-digest") == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["ok"] is True
    assert "summary file not written" in captured.err
    assert list(target.iterdir()) == []
