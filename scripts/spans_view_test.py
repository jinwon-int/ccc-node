"""Hermetic tests for scripts/spans-view.py and scripts/spans_view_probe.py (#2858 item 4).

Every fixture is a synthetic rollup summary staged in a temp folder, so each
assertion is about the hub's own contract — body-free pages, escaping, what a
node that did not answer looks like, the schema and size gates, atomic writes
and retention — never about this host's real spans.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location("spans_view", HERE / "spans-view.py")
assert _SPEC is not None and _SPEC.loader is not None
view = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(view)
PROBE = HERE / "spans_view_probe.py"

DAY = "2026-11-23"
NOW = "2026-11-24T04:30:00+09:00"  # the timer's run for DAY; yesterday (KST) == DAY
BODY = "SECRET BODY TEXT please never copy me"


def summary(node="alpha", day=DAY, **over):
    data = {
        "schema": "ccc.spans-rollup.v1", "date": day, "tz": "Asia/Seoul", "node": node, "empty": False,
        "totals": {"spans": 12, "turns": 3, "tool_calls": 9, "tool_errors": 1,
                   "tool_error_rate_pct": 11.11, "by_kind": {"tool": 9, "turn": 3},
                   "by_channel": {"hande": 3}, "by_tool_scope": {"main": 9}},
        "tools": {"Bash": {"calls": 7, "errors": 1, "error_rate_pct": 14.29, "p50_ms": 120.5, "p95_ms": 900.0},
                  "Read": {"calls": 2, "errors": 0, "error_rate_pct": 0.0, "p50_ms": None, "p95_ms": None}},
        "models": {"model-a": {"turns": 3, "input_tokens": 1000, "output_tokens": 2000,
                               "cache_read_tokens": 50000, "cache_creation_tokens": 10, "cost_usd": 1.25}},
        "turn_outcomes": {"status": {"ok": 3}, "outcome": {}, "error_type": {}},
        "skills": {"wiki-record": 1},
        "cost_ledger_check": {"available": True, "max_diff_pct": 0.0, "target_pct": 5.0,
                              "within_target": True, "ledger_zero_cost_rows": 0,
                              "metrics": {"turns": {"spans": 3, "ledger": 3, "diff_pct": 0.0}}},
        "read": {"files_read": 1, "corrupt_lines": 0, "rejected_spans": 0},
    }
    data.update(over)
    return data


def empty_summary(node="alpha", day=DAY):
    return summary(node, day, empty=True, tools={}, models={}, skills={},
                   totals={"spans": 0, "turns": 0, "tool_calls": 0, "tool_errors": 0,
                           "tool_error_rate_pct": 0.0, "by_kind": {}, "by_channel": {}},
                   cost_ledger_check={"available": False, "reason": "cost ledger missing or unreadable"})


def probe_text(obj=None, status="ok", folder="1", complete=True):
    lines = [f"SPANS_VIEW_STATUS={status}", f"SPANS_VIEW_FOLDER={folder}"]
    if obj is not None:
        lines.append("SPANS_VIEW_JSON=" + json.dumps(obj, ensure_ascii=True, separators=(",", ":")))
    if complete:
        lines.append("PROBE_COMPLETE=1")
    return "\n".join(lines) + "\n"


def stage(staging: Path, node: str, text: str = "", transport: str = "ok", rc: int = 0):
    staging.mkdir(parents=True, exist_ok=True)
    (staging / f"{node}.out").write_text(text, encoding="utf-8")
    (staging / f"{node}.meta").write_text(f"transport={transport} rc={rc}\n", encoding="utf-8")


class Env:
    def __init__(self, tmp_path: Path, monkeypatch):
        self.state = tmp_path / "hub-state"
        self.state.mkdir()
        self.staging = tmp_path / "staging"
        self.out = tmp_path / "uploads" / "spans"
        self.out.parent.mkdir()
        self.data = self.state / "spans-view" / "data"
        self.monkeypatch = monkeypatch
        monkeypatch.setenv("CCC_STATE_DIR", str(self.state))
        monkeypatch.setenv("CCC_SPANS_VIEW_NOW", NOW)
        monkeypatch.delenv("CCC_FLEET_NODES", raising=False)

    def at(self, day: str):
        """Move the clock to the timer run that follows ``day``."""
        after = date.fromisoformat(day) + timedelta(days=1)
        self.monkeypatch.setenv("CCC_SPANS_VIEW_NOW", f"{after.isoformat()}T04:30:00+09:00")

    def build(self, capsys, nodes, day=DAY, staged=True, extra=()):
        argv = ["build", "--out-dir", str(self.out), "--date", day, "--nodes", " ".join(nodes), *extra]
        if staged:
            argv += ["--staging", str(self.staging)]
        rc = view.main(argv)
        lines = capsys.readouterr().out.strip().splitlines()
        return rc, lines, json.loads(lines[-1])

    def page(self, name=f"{DAY}.html"):
        return (self.out / name).read_text(encoding="utf-8")

    def status(self, day=DAY):
        return json.loads((self.data / day / "_status.json").read_text(encoding="utf-8"))["nodes"]


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


# ------------------------------------------------------------------ contract


def test_pages_carry_no_body_text_and_unknown_keys_are_dropped(env, capsys):
    raw = summary()
    raw["prompt"] = BODY
    raw["totals"]["note"] = BODY
    raw["tools"]["Bash"]["input"] = BODY
    raw["models"]["model-a"]["last_message"] = BODY
    raw["cost_ledger_check"]["note"] = BODY
    raw["read"]["sample_line"] = BODY
    stage(env.staging, "alpha", probe_text(raw))
    rc, _, result = env.build(capsys, ["alpha"])
    assert rc == 0 and result["collected"] == 1
    record_text = (env.data / DAY / "alpha.json").read_text(encoding="utf-8")
    for text in (env.page(), env.page("index.html"), record_text):
        assert BODY not in text and "SECRET" not in text
    record = json.loads(record_text)
    assert "prompt" not in record and "note" not in record["totals"]
    assert set(record["tools"][0]) == {"name", "calls", "errors", "error_rate_pct", "p50_ms", "p95_ms"}
    assert "노드 alpha" in env.page() and "Bash" in env.page() and "$1.25" in env.page()


def test_every_label_is_escaped_and_no_script_can_reach_a_page(env, capsys):
    script = "<script>alert(1)</script>"
    img = '"><img src=x onerror=alert(2)>'
    raw = summary(node="<b>evil</b>")
    raw["tools"] = {script: {"calls": 5, "errors": 0, "error_rate_pct": 0.0, "p50_ms": 1.0, "p95_ms": 2.0}}
    raw["models"] = {img: {"turns": 1, "input_tokens": 1, "output_tokens": 1, "cost_usd": 0.5}}
    raw["skills"] = {"a & b": 2}
    raw["totals"]["by_channel"] = {"</td><td onmouseover=x>": 1}
    stage(env.staging, "alpha", probe_text(raw))
    rc, _, _ = env.build(capsys, ["alpha"])
    assert rc == 0
    page = env.page()
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page and "a &amp; b" in page
    assert "&lt;b&gt;evil&lt;/b&gt;" in page  # the name the node reported, shown as text
    for needle in ("<script", "<img", "<b>evil", "onmouseover=x>"):
        assert needle not in page
    assert "&quot;&gt;&lt;img src=x onerror=alert(2)&gt;" in page
    assert "<script" not in env.page("index.html")
    assert "Content-Security-Policy" in page and "default-src 'none'" in page


@pytest.mark.parametrize("bad", ["<script>", "a/b", "../x", "a b;rm", ".hidden", "x" * 80, "naïve"])
def test_a_node_name_that_is_not_a_plain_name_stops_the_run(env, capsys, bad):
    rc = view.main(["build", "--out-dir", str(env.out), "--date", DAY, "--nodes", f"alpha {bad}"])
    out = capsys.readouterr().out
    assert rc == 2 and json.loads(out.strip().splitlines()[-1])["ok"] is False
    assert not env.out.exists() and not env.data.exists()


def test_nodes_that_did_not_answer_are_shown_as_not_collected_with_the_reason(env, capsys):
    stage(env.staging, "alpha", probe_text(summary()))
    stage(env.staging, "down", transport="unreachable", rc=255)
    stage(env.staging, "slow", transport="timeout", rc=124)
    stage(env.staging, "broken", transport="error", rc=1)
    stage(env.staging, "nofile", probe_text(status="missing", folder="1"))
    stage(env.staging, "old", probe_text(status="missing", folder="0"))
    stage(env.staging, "partial", probe_text(summary("partial"), complete=False))
    nodes = ["alpha", "down", "slow", "broken", "nofile", "old", "partial", "silent"]
    rc, lines, result = env.build(capsys, nodes)
    assert rc == 0, "missing nodes must not fail the run"
    assert result["collected"] == 1 and result["not_collected"] == nodes[1:]
    assert "SPANS_VIEW UNREACHABLE down ssh-255" in lines
    assert "SPANS_VIEW UNVERIFIED slow timeout-124" in lines
    assert "SPANS_VIEW UNVERIFIED silent no-result" in lines
    page = env.page()
    for text in ("UNREACHABLE — ssh 연결 실패(255)", "UNVERIFIED — 제한 시간 안에 응답 없음(124)",
                 "UNVERIFIED — 프로브 오류", "그날 집계 파일 없음", "집계 폴더 없음",
                 "UNVERIFIED — 프로브가 끝까지 돌지 않음", "UNVERIFIED — 수집 결과 없음"):
        assert "미수집 — " + text in page, text
    for node in nodes:
        assert f"노드 {node}" in page
    assert env.page("index.html").count("<td>미수집</td>") == 7
    status = env.status()
    assert status["down"] == {"state": "unreachable", "detail": "ssh-255"}
    assert status["slow"] == {"state": "unverified", "detail": "timeout-124"}
    assert not (env.data / DAY / "partial.json").exists()


def test_an_idle_day_is_collected_data_not_a_gap(env, capsys):
    stage(env.staging, "alpha", probe_text(empty_summary()))
    rc, lines, result = env.build(capsys, ["alpha"])
    assert rc == 0 and result["collected"] == 1 and result["not_collected"] == []
    assert "SPANS_VIEW EMPTY alpha" in lines
    page = env.page()
    assert "활동 없음" in page and "미수집" not in page
    assert "<td>0</td>" in env.page("index.html")


@pytest.mark.parametrize("mutate, detail", [
    (lambda s: s.update(schema="ccc.spans-rollup.v2"), "schema"),
    (lambda s: s.pop("schema"), "schema"),
    (lambda s: s.update(date="2026-11-22"), "date"),
    (lambda s: s.update(empty="no"), "schema"),
    (lambda s: s["totals"].update(turns="3"), "schema"),
    (lambda s: s["totals"].update(turns=True), "schema"),
    (lambda s: s["totals"].update(turns=-1), "schema"),
    (lambda s: s["totals"].update(tool_error_rate_pct=float("nan")), "schema"),
    (lambda s: s["totals"].update(tool_error_rate_pct=250.0), "schema"),
    (lambda s: s.update(tools=["Bash"]), "schema"),
    (lambda s: s["tools"].update(Bash="seven"), "schema"),
    (lambda s: s["models"]["model-a"].update(cost_usd=float("inf")), "schema"),
    (lambda s: s.update(skills={"x": "many"}), "schema"),
    (lambda s: s.update(node=7), "schema"),
    (lambda s: s["cost_ledger_check"].update(within_target="yes"), "schema"),
    (lambda s: s.update(totals=None), "schema"),
])
def test_a_summary_that_breaks_the_schema_is_rejected_whole(env, capsys, mutate, detail):
    raw = summary()
    mutate(raw)
    # NaN/Infinity are not JSON, but Python's own dump/load round-trips them.
    staged = probe_text(status="ok") .replace("PROBE_COMPLETE=1\n", "") \
        + "SPANS_VIEW_JSON=" + json.dumps(raw) + "\nPROBE_COMPLETE=1\n"
    stage(env.staging, "alpha", staged)
    rc, lines, result = env.build(capsys, ["alpha"])
    assert rc == 0 and result["collected"] == 0
    assert f"SPANS_VIEW INVALID alpha {detail}" in lines
    assert not (env.data / DAY / "alpha.json").exists(), "a rejected summary leaves no record"
    assert "미수집 — " in env.page() and "받은 내용을 버렸습니다" in env.page()


@pytest.mark.parametrize("text", ["SPANS_VIEW_STATUS=ok\nSPANS_VIEW_JSON={not json\nPROBE_COMPLETE=1\n",
                                  "SPANS_VIEW_STATUS=ok\nSPANS_VIEW_JSON=[1,2]\nPROBE_COMPLETE=1\n",
                                  "SPANS_VIEW_STATUS=ok\nPROBE_COMPLETE=1\n",
                                  "SPANS_VIEW_STATUS=not-json\nPROBE_COMPLETE=1\n"])
def test_an_answer_that_is_not_a_json_object_is_rejected(env, capsys, text):
    stage(env.staging, "alpha", text)
    rc, lines, _ = env.build(capsys, ["alpha"])
    assert rc == 0 and any(line.startswith("SPANS_VIEW INVALID alpha") for line in lines)


def test_size_caps_reject_an_oversized_answer_at_every_layer(env, capsys):
    padded = summary()
    padded["skills"] = {f"skill-{i:06d}-" + "x" * 80: 1 for i in range(30000)}
    big = probe_text(padded)
    assert len(big.encode()) > view.MAX_STAGED_BYTES
    stage(env.staging, "bigfile", big)                                  # staged answer over the cap
    stage(env.staging, "cut", transport="oversize", rc=141)            # collector cut the stream
    stage(env.staging, "nodecap", probe_text(status="oversize"))       # node refused its own file
    rc, lines, result = env.build(capsys, ["bigfile", "cut", "nodecap"])
    assert rc == 0 and result["collected"] == 0
    for node in ("bigfile", "cut", "nodecap"):
        assert f"SPANS_VIEW OVERSIZE {node} size" in lines
    assert env.page().count("크기 상한 초과") == 6  # summary row + node section, per node
    assert list((env.data / DAY).glob("*.json")) == [env.data / DAY / "_status.json"]


def test_lists_and_labels_are_capped(env, capsys):
    raw = summary()
    raw["tools"] = {f"tool-{i:04d}": {"calls": i, "errors": 0, "error_rate_pct": 0.0,
                                      "p50_ms": None, "p95_ms": None} for i in range(1, 400)}
    raw["skills"] = {"s" * 500: 3, "ctrl\x00\x1b[31mname\n": 2}
    stage(env.staging, "alpha", probe_text(raw))
    rc, _, _ = env.build(capsys, ["alpha"])
    assert rc == 0
    record = json.loads((env.data / DAY / "alpha.json").read_text(encoding="utf-8"))
    assert len(record["tools"]) == view.MAX_TOOLS and record["tools"][0]["name"] == "tool-0399"
    assert max(len(name) for name in record["skills"]) == view.MAX_LABEL
    assert all(ch.isprintable() for name in record["skills"] for ch in name)
    assert env.page().count("<tr><td>tool-") == view.TOOL_ROWS


def test_a_failed_rerun_never_replaces_a_collected_record(env, capsys):
    stage(env.staging, "alpha", probe_text(summary()))
    assert env.build(capsys, ["alpha"])[2]["collected"] == 1
    stage(env.staging, "alpha", transport="unreachable", rc=255)
    rc, lines, result = env.build(capsys, ["alpha"])
    assert rc == 0 and result["collected"] == 1 and "SPANS_VIEW OK alpha" in lines
    assert env.status()["alpha"]["state"] == "ok" and "미수집" not in env.page()


def test_pages_can_be_rebuilt_from_the_kept_records_without_staging(env, capsys):
    stage(env.staging, "alpha", probe_text(summary()))
    env.build(capsys, ["alpha"])
    (env.out / f"{DAY}.html").unlink()
    rc, _, result = env.build(capsys, ["alpha", "beta"], staged=False)
    assert rc == 0 and result["collected"] == 1 and result["not_collected"] == ["beta"]
    assert "노드 alpha" in env.page() and "미수집 — UNVERIFIED — 수집 결과 없음" in env.page()


def test_one_hostile_answer_costs_that_node_only(env, capsys):
    huge = summary("evil")
    huge["models"]["model-a"]["cost_usd"] = int("1" + "0" * 400)   # too large for a float
    deep = "SPANS_VIEW_STATUS=ok\nSPANS_VIEW_JSON=" + "[" * 100000 + "]" * 100000 + "\nPROBE_COMPLETE=1\n"
    stage(env.staging, "good", probe_text(summary("good")))
    stage(env.staging, "evil", probe_text(huge))
    stage(env.staging, "deep", deep)
    rc, lines, result = env.build(capsys, ["good", "evil", "deep"])
    assert rc == 0 and result["collected"] == 1 and result["not_collected"] == ["evil", "deep"]
    assert "SPANS_VIEW OK good" in lines
    assert any(line.startswith("SPANS_VIEW INVALID evil") for line in lines)
    assert any(line.startswith("SPANS_VIEW INVALID deep") for line in lines)
    assert env.status()["good"]["state"] == "ok" and "노드 good" in env.page()


def test_an_unforeseen_error_in_one_answer_does_not_abort_the_run(env, capsys, monkeypatch):
    real = view.read_staged

    def flaky(staging, node, day):
        if node == "boom":
            raise RuntimeError("unforeseen")
        return real(staging, node, day)

    monkeypatch.setattr(view, "read_staged", flaky)
    stage(env.staging, "good", probe_text(summary("good")))
    stage(env.staging, "boom", probe_text(summary("boom")))
    rc, lines, result = env.build(capsys, ["boom", "good"])
    assert rc == 0 and result["collected"] == 1 and "SPANS_VIEW INVALID boom schema" in lines


def test_staged_files_that_are_symlinks_are_not_followed(env, capsys, tmp_path):
    real = tmp_path / "real.out"
    real.write_text(probe_text(summary()), encoding="utf-8")
    env.staging.mkdir()
    (env.staging / "alpha.out").symlink_to(real)
    (env.staging / "alpha.meta").write_text("transport=ok rc=0\n", encoding="utf-8")
    meta = tmp_path / "real.meta"
    meta.write_text("transport=ok rc=0\n", encoding="utf-8")
    (env.staging / "beta.out").write_text(probe_text(summary("beta")), encoding="utf-8")
    (env.staging / "beta.meta").symlink_to(meta)
    rc, lines, result = env.build(capsys, ["alpha", "beta"])
    assert rc == 0 and result["collected"] == 0
    assert "SPANS_VIEW UNVERIFIED alpha no-result" in lines
    assert "SPANS_VIEW UNVERIFIED beta no-result" in lines


# ------------------------------------------------------------ files and dates


def test_modes_atomic_writes_and_no_leftover_temp_files(env, capsys):
    stage(env.staging, "alpha", probe_text(summary()))
    env.build(capsys, ["alpha"])

    def mode(path):
        return stat.S_IMODE(path.stat().st_mode)

    assert mode(env.out) == 0o755
    assert mode(env.out / f"{DAY}.html") == 0o644 and mode(env.out / "index.html") == 0o644
    assert mode(env.data) == 0o700 and mode(env.data / DAY) == 0o700
    assert mode(env.data / DAY / "alpha.json") == 0o600
    assert mode(env.data / DAY / "_status.json") == 0o600
    assert sorted(p.name for p in env.out.iterdir()) == [f"{DAY}.html", "index.html"]
    assert not list(env.data.rglob("*.tmp"))


def test_a_symlinked_output_folder_is_refused_and_its_target_untouched(env, capsys, tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    env.out.symlink_to(target)
    stage(env.staging, "alpha", probe_text(summary()))
    rc = view.main(["build", "--out-dir", str(env.out), "--date", DAY, "--nodes", "alpha",
                    "--staging", str(env.staging)])
    assert rc == 3 and json.loads(capsys.readouterr().out.strip().splitlines()[-1])["ok"] is False
    assert list(target.iterdir()) == []


def test_an_output_folder_reached_through_a_symlinked_parent_is_refused(env, capsys, tmp_path):
    victim = tmp_path / "victim"
    victim.mkdir()
    (tmp_path / "webroot").symlink_to(victim)
    stage(env.staging, "alpha", probe_text(summary()))
    rc = view.main(["build", "--out-dir", str(tmp_path / "webroot" / "spans"), "--date", DAY,
                    "--nodes", "alpha", "--staging", str(env.staging)])
    capsys.readouterr()
    assert rc == 3 and list(victim.iterdir()) == []


def test_a_group_or_world_writable_output_folder_is_refused(env, capsys):
    env.out.mkdir()
    for mode in (0o777, 0o775, 0o757):
        env.out.chmod(mode)
        rc = view.main(["build", "--out-dir", str(env.out), "--date", DAY, "--nodes", "alpha"])
        capsys.readouterr()
        assert rc == 3 and list(env.out.iterdir()) == [], oct(mode)


@pytest.mark.skipif(os.geteuid() != 0, reason="needs root to hand the folder to another uid")
def test_an_output_folder_owned_by_a_stranger_is_refused_but_the_parents_owner_is_accepted(
        env, capsys):
    env.out.mkdir()
    os.chown(env.out, 65534, 65534)
    rc = view.main(["build", "--out-dir", str(env.out), "--date", DAY, "--nodes", "alpha"])
    capsys.readouterr()
    assert rc == 3 and list(env.out.iterdir()) == []
    # The web app owns the parent and, after a restart, may own the folder too.
    os.chown(env.out.parent, 65534, 65534)
    stage(env.staging, "alpha", probe_text(summary()))
    assert env.build(capsys, ["alpha"])[0] == 0 and "노드 alpha" in env.page()


def test_swapping_the_folder_after_it_was_opened_cannot_redirect_a_write_or_a_delete(tmp_path):
    parent = tmp_path / "webroot"
    parent.mkdir()
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "index.html").write_text("victim index", encoding="utf-8")
    (victim / "2020-01-01.html").write_text("victim page", encoding="utf-8")
    out = view.OutDir(parent / "spans")
    try:
        (parent / "spans").rename(parent / "spans.real")
        (parent / "spans").symlink_to(victim)          # the swap
        out.write("index.html", "ours")
        assert out.prune(date(2026, 1, 1)) == 0
        assert not out.is_page("2020-01-01.html")
    finally:
        out.close()
    assert (victim / "index.html").read_text(encoding="utf-8") == "victim index"
    assert (victim / "2020-01-01.html").exists()
    assert (parent / "spans.real" / "index.html").read_text(encoding="utf-8") == "ours"
    assert sorted(p.name for p in (parent / "spans.real").iterdir()) == ["index.html"]


def test_a_missing_parent_of_the_output_folder_is_not_created(env, capsys, tmp_path):
    rc = view.main(["build", "--out-dir", str(tmp_path / "no" / "such" / "spans"), "--date", DAY,
                    "--nodes", "alpha"])
    capsys.readouterr()
    assert rc == 3 and not (tmp_path / "no").exists()


def test_a_page_that_is_a_symlink_is_replaced_not_followed(env, capsys, tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me", encoding="utf-8")
    env.out.mkdir()
    (env.out / "index.html").symlink_to(victim)
    stage(env.staging, "alpha", probe_text(summary()))
    assert env.build(capsys, ["alpha"])[0] == 0
    assert victim.read_text(encoding="utf-8") == "keep me"
    assert not (env.out / "index.html").is_symlink() and "스팬 롤업" in env.page("index.html")


def test_ninety_day_retention_drops_only_dated_pages_and_records(env, capsys, tmp_path):
    day = date.fromisoformat(DAY)
    old, edge = (day - timedelta(days=90)).isoformat(), (day - timedelta(days=89)).isoformat()
    for name in (old, edge):
        env.at(name)
        stage(env.staging, "alpha", probe_text(summary(day=name)))
        assert env.build(capsys, ["alpha"], day=name)[0] == 0
    (env.out / "notes.html").write_text("mine", encoding="utf-8")
    outside = tmp_path / "outside.html"
    outside.write_text("mine", encoding="utf-8")
    (env.out / "2020-01-01.html").symlink_to(outside)
    (env.data / "keep-me").mkdir()
    env.at(DAY)
    stage(env.staging, "alpha", probe_text(summary()))
    rc, _, result = env.build(capsys, ["alpha"])
    assert rc == 0 and result["pruned"] == 2
    assert not (env.out / f"{old}.html").exists() and not (env.data / old).exists()
    assert (env.out / f"{edge}.html").exists() and (env.data / edge / "alpha.json").exists()
    assert (env.out / "notes.html").exists() and outside.exists() and (env.data / "keep-me").is_dir()
    assert (env.out / "2020-01-01.html").is_symlink(), "a dated name that is a symlink is not ours"
    kept = sorted(p.name for p in env.out.glob("20*-*-*.html") if not p.is_symlink())
    assert kept == [f"{edge}.html", f"{DAY}.html"]


@pytest.mark.parametrize("bad, why", [("2027-11-23", "later than yesterday"),
                                      ("2026-11-24", "later than yesterday"),
                                      ("2026-08-25", "older than the retention window")])
def test_a_day_outside_the_window_is_refused_and_prunes_nothing(env, capsys, bad, why):
    stage(env.staging, "alpha", probe_text(summary()))
    env.build(capsys, ["alpha"])
    before = sorted(p.name for p in env.out.iterdir()), sorted(p.name for p in env.data.iterdir())
    rc = view.main(["build", "--out-dir", str(env.out), "--date", bad, "--nodes", "alpha"])
    error = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rc == 2 and why in error["error"]
    assert (sorted(p.name for p in env.out.iterdir()), sorted(p.name for p in env.data.iterdir())) == before


def test_rebuilding_an_older_day_keeps_newer_pages_and_the_index_window(env, capsys):
    older = "2026-11-10"
    stage(env.staging, "alpha", probe_text(summary()))
    env.build(capsys, ["alpha"])
    stage(env.staging, "alpha", probe_text(summary(day=older)))
    rc, _, result = env.build(capsys, ["alpha"], day=older)
    assert rc == 0 and result["pruned"] == 0 and result["date"] == older
    assert (env.out / f"{DAY}.html").exists() and (env.out / f"{older}.html").exists()
    index = env.page("index.html")
    first_row = index.split("<tbody>")[1].split("</tr>")[0]
    assert f'href="{DAY}.html"' in first_row, "the table still starts at yesterday"
    assert f'<a href="{older}.html">' in index and index.count("<tr><td>") == 30


def test_index_lists_thirty_days_and_links_only_days_that_have_a_page(env, capsys):
    previous = "2026-11-22"
    stage(env.staging, "alpha", probe_text(summary(day=previous)))
    env.build(capsys, ["alpha"], day=previous)
    stage(env.staging, "alpha", probe_text(summary()))
    env.build(capsys, ["alpha"])
    index = env.page("index.html")
    assert index.count("<tr><td>") == 30
    assert f'<a href="{DAY}.html">{DAY}</a>' in index and f'<a href="{previous}.html">' in index
    assert index.count("<a href=") == 2 and "<td>2026-11-21</td><td>–</td>" in index
    (env.out / f"{previous}.html").unlink()
    (env.out / f"{previous}.html").symlink_to(env.out / f"{DAY}.html")
    env.build(capsys, ["alpha"], staged=False)
    assert env.page("index.html").count("<a href=") == 1, "a symlinked page is not linked"
    assert "3턴 · $1.25" in index
    assert "://" not in index and "//" not in index.replace("</", "")


def test_default_day_is_yesterday_in_korea_time():
    now = datetime(2026, 11, 23, 19, 30, tzinfo=timezone.utc)  # 2026-11-24 04:30 KST
    assert view.parse_day(None, now) == date(2026, 11, 23)
    assert view.parse_day("2026-11-23", now) == date(2026, 11, 23)
    assert view.parse_day("2026-08-26", now) == date(2026, 8, 26)  # 89 days back: still kept
    for bad in ("2026-13-01", "2026-1-1", "yesterday", "2026-11-23;x", "2026-11-24", "2026-08-25"):
        with pytest.raises(view.UsageError):
            view.parse_day(bad, now)


def test_many_nodes_still_fit_the_builder_section_limit(env, capsys):
    nodes = [f"node{i:02d}" for i in range(30)]
    for node in nodes:
        stage(env.staging, node, probe_text(summary(node)))
    rc, _, result = env.build(capsys, nodes)
    assert rc == 0 and result["collected"] == 30
    page = env.page()
    assert all(f"노드 {node}</h2>" in page for node in nodes)


# --------------------------------------------------------------------- probe


def run_probe(state: Path, day=DAY, home: Path | None = None, path_prefix: Path | None = None,
              explicit=True, spools: Path | None = None, cwd: Path | None = None):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home or state),
           "CCC_SPANS_VIEW_CRON_SPOOLS": str(spools) if spools is not None else ""}
    if explicit:
        env["CCC_SPANS_VIEW_STATE_DIR"] = str(state)
    if path_prefix is not None:
        env["PATH"] = f"{path_prefix}:{env['PATH']}"
    done = subprocess.run([sys.executable, "-", day], stdin=PROBE.open("rb"), capture_output=True,
                          env=env, timeout=30, check=False, cwd=cwd)
    lines = done.stdout.decode("utf-8").splitlines()
    return done.returncode, lines, dict(line.partition("=")[::2] for line in lines)


def write_rollup(state: Path, obj, day=DAY) -> Path:
    folder = state / "spans-rollup"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{day}.json"
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) if not isinstance(obj, str) else obj,
                    encoding="utf-8")
    return path


def snapshot(root: Path):
    return sorted((str(p.relative_to(root)), p.stat().st_mtime_ns, p.stat().st_size)
                  for p in root.rglob("*"))


def test_probe_prints_the_summary_on_one_ascii_line_and_writes_nothing(tmp_path):
    raw = summary(node="노드 한글\nline")
    write_rollup(tmp_path, raw)
    before = snapshot(tmp_path)
    rc, lines, fields = run_probe(tmp_path)
    assert rc == 0 and lines[-1] == "PROBE_COMPLETE=1" and len(lines) == 4
    assert fields["SPANS_VIEW_STATUS"] == "ok" and fields["SPANS_VIEW_FOLDER"] == "1"
    assert fields["SPANS_VIEW_JSON"].isascii() and json.loads(fields["SPANS_VIEW_JSON"]) == raw
    assert snapshot(tmp_path) == before, "the probe is read-only"
    # and the hub accepts exactly this text
    state, detail, record = view._from_probe("\n".join(lines) + "\n", DAY)
    assert (state, detail) == ("ok", "") and record["totals"]["turns"] == 3


def test_probe_reports_a_missing_file_and_whether_the_folder_exists(tmp_path):
    rc, _, fields = run_probe(tmp_path)
    assert rc == 0 and fields["SPANS_VIEW_STATUS"] == "missing" and fields["SPANS_VIEW_FOLDER"] == "0"
    (tmp_path / "spans-rollup").mkdir()
    _, lines, fields = run_probe(tmp_path)
    assert fields["SPANS_VIEW_STATUS"] == "missing" and fields["SPANS_VIEW_FOLDER"] == "1"
    assert "SPANS_VIEW_JSON" not in fields and lines[-1] == "PROBE_COMPLETE=1"


def test_probe_refuses_oversize_non_json_symlink_and_bad_dates(tmp_path):
    path = write_rollup(tmp_path, "x" * (256 * 1024 + 1))
    assert run_probe(tmp_path)[2]["SPANS_VIEW_STATUS"] == "oversize"
    path.write_text("{broken", encoding="utf-8")
    assert run_probe(tmp_path)[2]["SPANS_VIEW_STATUS"] == "not-json"
    path.write_text("[1, 2]", encoding="utf-8")
    assert run_probe(tmp_path)[2]["SPANS_VIEW_STATUS"] == "not-json"
    path.unlink()
    secret = tmp_path / "other.json"
    secret.write_text(json.dumps({"schema": "x", "body": BODY}), encoding="utf-8")
    path.symlink_to(secret)
    rc, lines, fields = run_probe(tmp_path)
    assert fields["SPANS_VIEW_STATUS"] == "unreadable" and BODY not in "\n".join(lines)
    for bad in ("", "2026-11-2", "../../etc/passwd", "2026-11-23.json"):
        rc, lines, fields = run_probe(tmp_path, day=bad)
        assert rc == 0 and fields == {"SPANS_VIEW_STATUS": "bad-date", "PROBE_COMPLETE": "1"}


def fake_crontab(folder: Path, body: str) -> Path:
    folder.mkdir(exist_ok=True)
    cron = folder / "crontab"
    cron.write_text("#!/bin/sh\ncat <<'EOF'\n" + body + "EOF\n", encoding="utf-8")
    cron.chmod(0o755)
    return folder


def test_probe_finds_the_state_folder_from_the_rollup_cron_line(tmp_path):
    state = tmp_path / "svc home" / "state"
    write_rollup(state, summary("alpha"))
    decoy = tmp_path / "decoy"
    write_rollup(decoy, summary("commented-out"))      # only reachable through the comment
    other = tmp_path / "other"
    write_rollup(other, summary("other-job"))          # named by a line that is not the rollup
    fake_bin = fake_crontab(tmp_path / "bin", (
        f"# 41 3 * * * CCC_STATE_DIR=\"{decoy}\" python3 /x/scripts/spans-rollup.py\n"
        f"29 3 * * * bash -lc 'CCC_STATE_DIR=\"{other}\" python3 \"/x/scripts/cost-ledger.py\"'\n"
        f"41 3 * * * bash -lc 'CCC_STATE_DIR=\"{state}\" python3 \"/x/scripts/spans-rollup.py\"'\n"))
    home = tmp_path / "login-home"
    home.mkdir()
    rc, _, fields = run_probe(state, home=home, path_prefix=fake_bin, explicit=False)
    assert rc == 0 and fields["SPANS_VIEW_STATUS"] == "ok"
    assert json.loads(fields["SPANS_VIEW_JSON"])["node"] == "alpha"


def test_probe_skips_a_cron_value_that_needs_shell_expansion(tmp_path):
    home = tmp_path / "home"
    write_rollup(home / ".claude" / "state", summary("default"))
    # What a literal reading of each cron value would open, relative to the
    # probe's working directory. Both exist, so a wrong pick is visible.
    cwd = tmp_path / "cwd"
    write_rollup(cwd / "$HOME" / "state", summary("literal-dollar"))
    write_rollup(cwd / "relative" / "state", summary("relative-path"))
    fake_bin = fake_crontab(tmp_path / "bin", (
        "41 3 * * * CCC_STATE_DIR=\"$HOME/state\" python3 /x/scripts/spans-rollup.py\n"
        "41 3 * * * CCC_STATE_DIR=relative/state python3 /x/scripts/spans-rollup.py\n"))
    rc, _, fields = run_probe(home, home=home, path_prefix=fake_bin, explicit=False, cwd=cwd)
    assert rc == 0 and json.loads(fields["SPANS_VIEW_JSON"])["node"] == "default"


def test_probe_does_not_follow_a_symlinked_folder(tmp_path):
    real = tmp_path / "real-folder"
    real.mkdir()
    (real / f"{DAY}.json").write_text(json.dumps(summary("leak")), encoding="utf-8")
    state = tmp_path / "state"
    state.mkdir()
    (state / "spans-rollup").symlink_to(real)
    rc, lines, fields = run_probe(state)
    assert rc == 0 and fields["SPANS_VIEW_STATUS"] == "missing" and fields["SPANS_VIEW_FOLDER"] == "0"
    assert "leak" not in "\n".join(lines)


def test_probe_keeps_looking_when_one_candidate_file_is_unusable(tmp_path):
    first = tmp_path / "first"
    (first / "spans-rollup").mkdir(parents=True)
    (first / "spans-rollup" / f"{DAY}.json").symlink_to(tmp_path / "nowhere.json")
    home = tmp_path / "home"
    write_rollup(home / ".claude" / "state", summary("second"))
    fake_bin = fake_crontab(tmp_path / "bin",
                            f"41 3 * * * CCC_STATE_DIR=\"{first}\" python3 /x/scripts/spans-rollup.py\n")
    rc, _, fields = run_probe(home, home=home, path_prefix=fake_bin, explicit=False)
    assert rc == 0 and fields["SPANS_VIEW_STATUS"] == "ok"
    assert json.loads(fields["SPANS_VIEW_JSON"])["node"] == "second"
    # with no better candidate the problem itself is reported
    (home / ".claude" / "state" / "spans-rollup" / f"{DAY}.json").unlink()
    rc, _, fields = run_probe(home, home=home, path_prefix=fake_bin, explicit=False)
    assert fields["SPANS_VIEW_STATUS"] == "unreadable" and "SPANS_VIEW_JSON" not in fields


@pytest.mark.skipif(os.geteuid() != 0, reason="other users' crontabs are read only as root")
def test_probe_as_root_uses_another_accounts_crontab_only_for_that_accounts_folder(tmp_path):
    import pwd
    stranger = pwd.getpwuid(65534).pw_name
    spool = tmp_path / "spool"
    spool.mkdir()
    theirs = tmp_path / "theirs"
    write_rollup(theirs, summary("service-account"))
    home = tmp_path / "home"
    home.mkdir()
    no_cron = fake_crontab(tmp_path / "bin", "")
    (spool / stranger).write_text(
        f"41 3 * * * CCC_STATE_DIR=\"{theirs}\" python3 /x/scripts/spans-rollup.py\n", encoding="utf-8")
    # The folder belongs to root, not to the crontab's owner: not accepted.
    rc, _, fields = run_probe(home, home=home, path_prefix=no_cron, explicit=False, spools=spool)
    assert rc == 0 and fields["SPANS_VIEW_STATUS"] == "missing" and fields["SPANS_VIEW_FOLDER"] == "0"
    # Handed to that account, the same line is the service account's rollup.
    for path in (theirs, theirs / "spans-rollup"):
        os.chown(path, 65534, 65534)
    rc, _, fields = run_probe(home, home=home, path_prefix=no_cron, explicit=False, spools=spool)
    assert fields["SPANS_VIEW_STATUS"] == "ok"
    assert json.loads(fields["SPANS_VIEW_JSON"])["node"] == "service-account"


def test_probe_falls_back_to_the_default_state_folder(tmp_path):
    home = tmp_path / "home"
    write_rollup(home / ".claude" / "state", summary())
    empty_bin = tmp_path / "bin"
    empty_bin.mkdir()
    (empty_bin / "crontab").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    (empty_bin / "crontab").chmod(0o755)
    rc, _, fields = run_probe(home, home=home, path_prefix=empty_bin, explicit=False)
    assert rc == 0 and fields["SPANS_VIEW_STATUS"] == "ok"
