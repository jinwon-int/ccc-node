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
