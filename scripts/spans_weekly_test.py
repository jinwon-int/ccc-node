"""Hermetic tests for scripts/spans-weekly.py (#2899).

Every fixture is a synthetic hub record folder (``<data>/<day>/<node>.json`` +
``_status.json``, written through spans-view.py's own normalizer) and a fake
``wiki-agent`` over a temp git worktree, so the assertions are about the
weekly contract — the section shape, escaping, the marker gate, archiving,
the no-data week, thresholds, dry-run — never about this host's spans.
"""

from __future__ import annotations

import fcntl
import importlib.util
import json
import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location("spans_weekly", HERE / "spans-weekly.py")
assert _SPEC is not None and _SPEC.loader is not None
weekly = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(weekly)
view = weekly.view
SCRIPT = HERE / "spans-weekly.py"

NOW = "2026-11-23T05:00:00+09:00"  # Monday; the last complete week is 2026-W47
W47 = date(2026, 11, 16)
W46 = date(2026, 11, 9)
NODES = ["node-a", "node-b", "node-c"]
BODY = "SECRET BODY TEXT please never copy me"


def summary(node, day, turns=10, calls=100, errors=1, cost=0.5, tools=None, a2a=None, ledger=None, empty=False):
    data = {
        "schema": "ccc.spans-rollup.v1", "date": day, "node": node, "empty": empty,
        "totals": {"spans": turns + calls, "turns": turns, "tool_calls": calls, "tool_errors": errors,
                   "tool_error_rate_pct": round(100.0 * errors / calls, 2) if calls else 0.0},
        "tools": tools if tools is not None else {
            "Bash": {"calls": calls, "errors": errors, "error_rate_pct": 0.0, "p50_ms": 10.0, "p95_ms": 500.0}},
        "models": {"model-a": {"turns": turns, "input_tokens": 1, "output_tokens": 1, "cost_usd": cost}},
        "cost_ledger_check": ledger if ledger is not None else {
            "available": True, "within_target": True, "max_diff_pct": 1.0, "target_pct": 5.0},
        "prompt": BODY,  # unknown key: dropped by the view's normalizer, never reaches the page
    }
    if a2a is not None:
        data["a2a"] = a2a
    return data


def put_day(data: Path, day: str, nodes: dict):
    """``nodes``: name -> summary dict (collected) or (state, detail) (not collected)."""
    folder = data / day
    folder.mkdir(parents=True, exist_ok=True)
    status = {}
    for node, value in nodes.items():
        if isinstance(value, tuple):
            status[node] = {"state": value[0], "detail": value[1]}
            continue
        record = view.normalize_summary(value, day)
        (folder / f"{node}.json").write_text(json.dumps(record), encoding="utf-8")
        status[node] = {"state": "empty" if record["empty"] else "ok", "detail": ""}
    (folder / "_status.json").write_text(json.dumps({"schema": view.STATUS_SCHEMA, "date": day,
                                                     "nodes": status}), encoding="utf-8")


def put_week(data: Path, monday: date, make):
    for offset in range(7):
        day = (monday + timedelta(days=offset)).isoformat()
        put_day(data, day, make(day, offset))


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("CCC_SPANS_VIEW_NOW", NOW)
    for name in ("CCC_FLEET_NODES", "CCC_SPANS_WEEKLY_ERROR_PP", "CCC_SPANS_WEEKLY_TURNS_LOW",
                 "CCC_SPANS_WEEKLY_TURNS_HIGH", "CCC_SPANS_WEEKLY_PAGE"):
        monkeypatch.delenv(name, raising=False)
    folder = tmp_path / "state" / "spans-view" / "data"
    folder.mkdir(parents=True)
    return folder


def standard_fleet(data: Path):
    """W46 and W47 for three nodes: node-a busy, node-b with gaps, node-c silent."""
    def w46(day, i):
        return {"node-a": summary("node-a", day, turns=10, calls=100, errors=1),
                "node-b": summary("node-b", day, turns=10, calls=100, errors=1),
                "node-c": ("missing", "no-folder")}

    def w47(day, i):
        nodes = {"node-a": summary("node-a", day, turns=10, calls=100, errors=3,
                                   a2a={"spans": 3, "ok": 2, "failed": 1}),
                 "node-c": ("missing", "no-folder")}
        if i < 4:
            nodes["node-b"] = summary("node-b", day, turns=30, calls=100, errors=1)
        elif i < 6:
            nodes["node-b"] = ("unreachable", "ssh-255")
        return nodes  # Sunday: node-b absent from the status file (no-result)
    put_week(data, W46, w46)
    put_week(data, W47, w47)


def dry_run(data: Path, capsys, *extra) -> tuple[int, str]:
    rc = weekly.main(["--dry-run", "--data-dir", str(data), *extra])
    return rc, capsys.readouterr().out


# ------------------------------------------------------------------ section


def test_dry_run_prints_the_previous_week_and_is_stable(data, capsys):
    standard_fleet(data)
    rc, out = dry_run(data, capsys, "--nodes", " ".join(NODES))
    assert rc == 0
    lines = out.splitlines()
    assert lines[0] == "<!-- span-weekly:2026-W47 -->"
    assert lines[1] == "## 2026-W47 · 2026-11-16 ~ 2026-11-22 (KST)"
    assert lines[3] == ("- 요약: 수집 노드 2/3 · 수집일 11/21 · 턴 190 · 도구 호출 1,100 · 도구 오류율 2.3% · "
                        "비용 $5.50 · 큰 변화 2건")
    assert "| 노드 | 수집일 | 턴 | 도구 호출 | 도구 오류율 | 비용(USD) | 원장 차이 최대 | a2a 성공/실패 |" in lines
    assert "| node-a | 7/7 | 70 | 700 | 3.0% | $3.50 | 1.0% | 14/7 |" in lines
    assert "| node-b | 4/7 | 120 | 400 | 1.0% | $2.00 | 1.0% | - |" in lines
    assert "| node-c | 0/7 | - | - | - | - | - | - |" in lines
    assert "| **플릿 합계** | 11/21 | 190 | 1,100 | 2.3% | $5.50 | - | 14/7 |" in lines
    assert "| `Bash` | 1,100 | 25 | 2.3% | 500 |" in lines
    assert "- node-a — 도구 오류율 1.0% → 3.0% (+2.0%p)" in lines
    assert "- node-b — 수집일 평균 턴 10.0 → 30.0 (3.00배)" in lines
    assert "- node-b — 3일: ssh-255 2 · no-result 1" in lines
    assert "- node-c — 7일: no-folder 7" in lines
    assert BODY not in out
    rc2, again = dry_run(data, capsys, "--nodes", " ".join(NODES))
    assert rc2 == 0 and again == out


def test_dry_run_never_calls_wiki_agent(data, capsys, tmp_path):
    standard_fleet(data)
    calls = tmp_path / "calls"
    agent = _exe(tmp_path / "wiki-agent", f"#!/usr/bin/env bash\necho \"$@\" >> {str(calls)!r}\n")
    rc, out = dry_run(data, capsys, "--nodes", "node-a", "--wiki-agent-bin", str(agent))
    assert rc == 0 and out.startswith("<!-- span-weekly:") and not calls.exists()


def test_a2a_column_only_when_some_node_reported_it(data, capsys):
    put_week(data, W47, lambda day, i: {"node-a": summary("node-a", day)})
    _, out = dry_run(data, capsys, "--nodes", "node-a")
    assert "a2a" not in out
    assert "| node-a | 7/7 | 70 | 700 | 1.0% | $3.50 | 1.0% |" in out.splitlines()


def test_nodes_default_to_the_status_files(data, capsys):
    standard_fleet(data)
    _, out = dry_run(data, capsys)
    assert "수집 노드 2/3" in out and "| node-c | 0/7 |" in out


def test_table_breaking_tool_names_stay_inside_their_cell(data, capsys):
    evil = {"x|y`z\n<!-- span-weekly:2026-W01 -->": {"calls": 5, "errors": 5, "error_rate_pct": 100.0},
            "[link](http://example.invalid)<b>": {"calls": 4, "errors": 2, "error_rate_pct": 50.0},
            "A" * 300: {"calls": 3, "errors": 1, "error_rate_pct": 33.3}}
    put_week(data, W47, lambda day, i: {"node-a": summary("node-a", day, tools=evil)})
    _, out = dry_run(data, capsys, "--nodes", "node-a")
    assert out.count("<!-- span-weekly:") == 1
    rows = [line for line in out.splitlines() if line.startswith("| `")]
    assert len(rows) == 3
    assert all(row.count("|") == 6 and row.count("`") == 2 for row in rows)
    body = "\n".join(out.splitlines()[1:])
    assert not any(ch in body for ch in "<>[]")
    assert max(len(row.split("`")[1]) for row in rows) == weekly.MAX_NAME


def test_week_with_no_collected_day_is_one_line(data, capsys):
    put_week(data, W47, lambda day, i: {"node-a": ("unreachable", "ssh-255"), "node-b": ("missing", "no-folder")})
    _, out = dry_run(data, capsys, "--nodes", "node-a node-b")
    assert out.splitlines() == ["<!-- span-weekly:2026-W47 -->", "## 2026-W47 · 2026-11-16 ~ 2026-11-22 (KST)", "",
                                "- 수집 없음 — 이 주에는 수집된 날이 없습니다 (노드 2 · no-folder 7 · ssh-255 7)."]


def test_hub_did_not_run_is_its_own_reason(data, capsys):
    _, out = dry_run(data, capsys, "--nodes", "node-a")
    assert "- 수집 없음 — 이 주에는 수집된 날이 없습니다 (노드 1 · no-run 7)." in out


def test_idle_days_count_as_collected(data, capsys):
    put_week(data, W47, lambda day, i: {"node-a": summary("node-a", day, turns=0, calls=0, errors=0, cost=0.0,
                                                        tools={}, empty=True,
                                                        ledger={"available": False})})
    _, out = dry_run(data, capsys, "--nodes", "node-a")
    assert "| node-a | 7/7 | 0 | 0 | - | $0.00 | 원장 없음 |" in out
    assert "도구 오류 없음." in out and "없음 — 모든 노드를 7일 모두 수집했습니다." in out


def test_corrupt_record_is_a_gap_not_a_crash(data, capsys):
    put_week(data, W47, lambda day, i: {"node-a": summary("node-a", day)})
    (data / "2026-11-18" / "node-a.json").write_text("{not json", encoding="utf-8")
    bad = json.loads((data / "2026-11-19" / "node-a.json").read_text())
    bad["totals"]["turns"] = -5
    (data / "2026-11-19" / "node-a.json").write_text(json.dumps(bad), encoding="utf-8")
    _, out = dry_run(data, capsys, "--nodes", "node-a")
    assert "| node-a | 5/7 |" in out and "- node-a — 2일: record-invalid 2" in out


def test_ledger_over_target_days_are_counted(data, capsys):
    def make(day, i):
        ledger = {"available": True, "within_target": i != 2, "max_diff_pct": 9.5 if i == 2 else 1.0}
        return {"node-a": summary("node-a", day, ledger=ledger)}
    put_week(data, W47, make)
    _, out = dry_run(data, capsys, "--nodes", "node-a")
    assert "| node-a | 7/7 | 70 | 700 | 1.0% | $3.50 | 9.5% (초과 1일) |" in out


# --------------------------------------------------------------- thresholds


def _two_weeks(data, before, after):
    put_week(data, W46, lambda day, i: {"node-a": summary("node-a", day, **before)})
    put_week(data, W47, lambda day, i: {"node-a": summary("node-a", day, **after)})


@pytest.mark.parametrize("before,after,flagged", [
    ({"errors": 1}, {"errors": 3}, True),                   # +2.0pp exactly
    ({"errors": 10}, {"errors": 11}, False),                # +1.0pp
    ({"errors": 3}, {"errors": 1}, False),                  # a drop is not a big change
    ({"turns": 10}, {"turns": 5}, True),                    # 0.5x exactly
    ({"turns": 10}, {"turns": 6}, False),
    ({"turns": 10}, {"turns": 20}, True),                   # 2x exactly
    ({"turns": 10}, {"turns": 19}, False),
])
def test_default_thresholds(data, capsys, before, after, flagged):
    _two_weeks(data, before, after)
    _, out = dry_run(data, capsys, "--nodes", "node-a")
    section = out.split("### 전주 대비 큰 변화")[1].split("### 미수집")[0]
    assert ("- node-a — " in section) is flagged
    assert ("큰 변화 1건" if flagged else "큰 변화 0건") in out


def test_thresholds_are_configurable_by_flag_and_env(data, capsys, monkeypatch):
    _two_weeks(data, {"errors": 10, "turns": 10}, {"errors": 11, "turns": 14})
    _, out = dry_run(data, capsys, "--nodes", "node-a")
    assert "큰 변화 0건" in out
    _, out = dry_run(data, capsys, "--nodes", "node-a", "--error-rate-pp", "1", "--turns-high", "1.4")
    assert "기준(잠정): 도구 오류율 +1%p 이상, 또는 수집일 평균 턴 0.5배 이하·1.4배 이상." in out
    assert "(+1.0%p)" in out and "(1.40배)" in out
    monkeypatch.setenv("CCC_SPANS_WEEKLY_ERROR_PP", "0.5")
    _, out = dry_run(data, capsys, "--nodes", "node-a")
    assert "+0.5%p 이상" in out and "큰 변화 1건" in out


@pytest.mark.parametrize("flags", [["--error-rate-pp", "0"], ["--turns-low", "1"], ["--turns-high", "1"],
                                   ["--turns-high", "500"], ["--catch-up", "12"]])
def test_out_of_range_settings_are_refused(data, capsys, flags):
    rc, out = dry_run(data, capsys, "--nodes", "node-a", *flags)
    assert rc == 2 and json.loads(out)["ok"] is False


def test_previous_week_without_records_is_not_compared(data, capsys):
    put_week(data, W47, lambda day, i: {"node-a": summary("node-a", day)})
    _, out = dry_run(data, capsys, "--nodes", "node-a")
    assert "전주(2026-W46) 수집 기록이 없어 비교하지 않았습니다." in out and "큰 변화 0건" in out


# ---------------------------------------------------------------------- weeks


def test_week_selection(data, capsys):
    now = view.datetime.fromisoformat(NOW)
    assert weekly.latest_week(now) == W47
    assert weekly.latest_week(view.datetime.fromisoformat("2026-11-29T23:00:00+09:00")) == W47  # Sunday
    assert weekly.parse_week("2026-W47") == W47
    assert weekly.parse_week("2026-W53") == date(2026, 12, 28)  # 2026 has 53 ISO weeks
    for bad in ("2025-W53", "2026-47", "2026-W5", "W47"):
        with pytest.raises(weekly.UsageError):
            weekly.parse_week(bad)
    for week, rc in (("2026-W48", 2), ("2026-W35", 2), ("2026-W36", 0), ("2026-W46", 0)):
        assert dry_run(data, capsys, "--nodes", "node-a", "--week", week)[0] == rc, week


def test_missing_records_folder_is_a_usage_error(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("CCC_SPANS_VIEW_NOW", NOW)
    rc = weekly.main(["--dry-run", "--state-dir", str(tmp_path / "nowhere")])
    assert rc == 2 and "records folder" in capsys.readouterr().out


def test_bad_node_names_are_refused(data, capsys):
    rc, out = dry_run(data, capsys, "--nodes", "node-a ../x")
    assert rc == 2


# ---------------------------------------------------------------- page merge


def section(week: str) -> str:
    return f"{weekly.marker(week)}\n## {week} · x (KST)\n\n- 요약: 주 {week}\n"


def test_merge_creates_page_with_digest_and_doc_id():
    pages: dict[str, str] = {}
    changes = weekly.merge_pages(weekly.DEFAULT_PAGE, lambda p: pages.get(p, ""),
                                 {"2026-W47": section("2026-W47")}, 121)
    text = changes[weekly.DEFAULT_PAGE]
    lines = text.splitlines()
    assert lines[0] == "# [DOC-121] 스팬 주간 플릿 요약 (자동 생성)"
    assert lines[2] == "> **에이전트 다이제스트** — status: `active` · 최신 주 2026-W47 · x (KST) — 주 2026-W47"
    assert text.count("<!-- span-weekly:2026-W47 -->") == 1 and list(changes) == [weekly.DEFAULT_PAGE]


def test_merge_is_a_no_op_for_a_week_already_present():
    pages = weekly.merge_pages(weekly.DEFAULT_PAGE, lambda p: "", {"2026-W47": section("2026-W47")}, 5)
    again = weekly.merge_pages(weekly.DEFAULT_PAGE, lambda p: pages.get(p, ""),
                               {"2026-W47": section("2026-W47") + "changed numbers\n"}, 6)
    assert again == {}


def test_merge_keeps_twelve_weeks_newest_first_and_archives_the_rest():
    weeks = [weekly.week_id(date(2026, 1, 5) + timedelta(weeks=k)) for k in range(12)]  # 2026-W02..W13
    pages: dict[str, str] = {}
    for week in weeks:
        pages.update(weekly.merge_pages(weekly.DEFAULT_PAGE, lambda p: pages.get(p, ""), {week: section(week)}, 40))
    main = pages[weekly.DEFAULT_PAGE]
    assert weekly.weeks_on(main) == set(weeks) and len(pages) == 1
    order = [w for w, _ in weekly.split_page(main)[1]]
    assert order == sorted(weeks, reverse=True)
    pages.update(weekly.merge_pages(weekly.DEFAULT_PAGE, lambda p: pages.get(p, ""),
                                    {"2026-W14": section("2026-W14")}, 41))
    archive = "pages/services/span-rollup-archive-2026.md"
    main = pages[weekly.DEFAULT_PAGE]
    assert "2026-W02" not in weekly.weeks_on(main) and "2026-W14" in weekly.weeks_on(main)
    assert len(weekly.weeks_on(main)) == 12 and main.startswith("# [DOC-40]")
    assert "최신 주 2026-W14" in main.splitlines()[2]
    assert pages[archive].startswith("# [DOC-41] 스팬 주간 플릿 요약 — 2026 보관")
    assert weekly.weeks_on(pages[archive]) == {"2026-W02"}
    # an archived week is not written back onto the page
    assert weekly.merge_pages(weekly.DEFAULT_PAGE, lambda p: pages.get(p, ""),
                              {"2026-W02": section("2026-W02")}, 42) == {}
    # the next move keeps the archive's id and newest-first order
    pages.update(weekly.merge_pages(weekly.DEFAULT_PAGE, lambda p: pages.get(p, ""),
                                    {"2026-W15": section("2026-W15")}, 42))
    assert pages[archive].startswith("# [DOC-41]")
    assert [w for w, _ in weekly.split_page(pages[archive])[1]] == ["2026-W03", "2026-W02"]


def test_archive_goes_by_iso_year():
    pages: dict[str, str] = {}
    start = date(2025, 10, 20)  # 2025-W43 .. 2026-W03 crosses the year
    for k in range(13):
        week = weekly.week_id(start + timedelta(weeks=k))
        pages.update(weekly.merge_pages(weekly.DEFAULT_PAGE, lambda p: pages.get(p, ""), {week: section(week)}, 7))
    assert weekly.weeks_on(pages["pages/services/span-rollup-archive-2025.md"]) == {"2025-W43"}


def test_merge_refuses_a_hand_made_page():
    with pytest.raises(weekly.WeeklyError, match="not made by this generator"):
        weekly.merge_pages(weekly.DEFAULT_PAGE, lambda p: "# Something else\n", {"2026-W47": section("2026-W47")}, 1)


# ------------------------------------------------------------------- publish


def _exe(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


def fake_wiki(tmp: Path) -> tuple[Path, Path, Path]:
    wt = tmp / "wiki"
    (wt / "pages" / "nodes").mkdir(parents=True)
    (wt / "pages" / "nodes" / "a.md").write_text("# [DOC-41] A\n\n## [ND-120] fact\n", encoding="utf-8")
    git = ["git", "-C", str(wt), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", str(wt)], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "base"], check=True)
    calls = tmp / "pr-calls"
    agent = _exe(tmp / "wiki-agent", f"""#!/usr/bin/env bash
case "$1" in
  write-path) echo "log line" >&2; echo {str(wt)!r} ;;
  pr) echo "pr $(pwd)" >> {str(calls)!r}; git -c user.email=t@t -c user.name=t add -A && \\
      git -c user.email=t@t -c user.name=t commit -qm weekly ;;
  *) exit 9 ;;
esac
""")
    return wt, agent, calls


def run_cli(data: Path, agent: Path, tmp: Path, *extra) -> tuple[int, dict, str]:
    result = tmp / "result.txt"
    cmd = [sys.executable, str(SCRIPT), "--data-dir", str(data), "--state-dir", str(tmp / "state"),
           "--wiki-agent-bin", str(agent), "--result-file", str(result), "--nodes", " ".join(NODES), *extra]
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "CCC_SPANS_VIEW_NOW": NOW}
    out = subprocess.run(cmd, capture_output=True, text=True, env=env)
    line = result.read_text(encoding="utf-8") if result.exists() else ""
    return out.returncode, json.loads(out.stdout.strip().splitlines()[-1]), line


def test_publish_adds_week_and_catch_up_then_skips(data, tmp_path):
    standard_fleet(data)
    wt, agent, calls = fake_wiki(tmp_path)
    rc, res, line = run_cli(data, agent, tmp_path)
    assert rc == 0 and res["changed"] and res["weeks"] == ["2026-W47", "2026-W46"]
    assert line == "스팬 주간 요약 PR 제출: 2026-W47, 2026-W46\n"
    page = (wt / weekly.DEFAULT_PAGE).read_text(encoding="utf-8")
    assert page.startswith("# [DOC-121] 스팬 주간 플릿 요약 (자동 생성)")
    assert "최신 주 2026-W47 · 2026-11-16 ~ 2026-11-22 (KST) — 수집 노드 2/3" in page.splitlines()[2]
    assert page.index("span-weekly:2026-W47") < page.index("span-weekly:2026-W46")
    assert oct((wt / weekly.DEFAULT_PAGE).stat().st_mode & 0o777) == "0o644"
    assert calls.read_text().count("pr ") == 1 and BODY not in page
    rc, res, line = run_cli(data, agent, tmp_path)
    assert rc == 0 and res["changed"] is False and line == "스팬 주간 요약 변경 없음: 2026-W47 이미 있음\n"
    assert calls.read_text().count("pr ") == 1
    assert (wt / weekly.DEFAULT_PAGE).read_text(encoding="utf-8").count("span-weekly:2026-W47") == 1


def test_publish_without_catch_up_adds_only_the_target_week(data, tmp_path):
    standard_fleet(data)
    wt, agent, calls = fake_wiki(tmp_path)
    rc, res, _ = run_cli(data, agent, tmp_path, "--catch-up", "0")
    assert rc == 0 and res["weeks"] == ["2026-W47"]


def test_publish_refuses_dirty_worktree_and_symlinked_page(data, tmp_path):
    standard_fleet(data)
    wt, agent, calls = fake_wiki(tmp_path)
    (wt / "pages" / "stray.md").write_text("x", encoding="utf-8")
    rc, res, line = run_cli(data, agent, tmp_path)
    assert rc == 1 and "not clean" in res["error"] and line.startswith("스팬 주간 요약 실패:")
    (wt / "pages" / "stray.md").unlink()
    (wt / "pages" / "services").mkdir()
    (wt / "pages" / "services" / "span-rollup.md").symlink_to(tmp_path / "elsewhere.md")
    subprocess.run(["git", "-C", str(wt), "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(wt), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "l"],
                   check=True)
    rc, res, _ = run_cli(data, agent, tmp_path)
    assert rc == 1 and "symlink" in res["error"] and not (tmp_path / "elsewhere.md").exists()
    assert not calls.exists()


def test_publish_refuses_a_bad_page_path(data, tmp_path):
    standard_fleet(data)
    wt, agent, calls = fake_wiki(tmp_path)
    rc, res, _ = run_cli(data, agent, tmp_path, "--page", "../etc/x.md")
    assert rc == 2 and not calls.exists()


def test_pr_failure_is_reported_and_retried_next_week(data, tmp_path, monkeypatch):
    standard_fleet(data)
    wt, agent, calls = fake_wiki(tmp_path)
    broken = _exe(tmp_path / "wiki-agent-broken", f"""#!/usr/bin/env bash
case "$1" in
  write-path) echo {str(wt)!r} ;;
  pr) git -C {str(wt)!r} checkout -q -- . ; git -C {str(wt)!r} clean -qfd ; exit 4 ;;
esac
""")
    rc, res, line = run_cli(data, broken, tmp_path, "--catch-up", "0")
    assert rc == 1 and "wiki-agent pr failed rc=4" in res["error"] and "실패" in line
    put_week(data, date(2026, 11, 23), lambda day, i: {"node-a": summary("node-a", day)})
    result = tmp_path / "r2"
    cmd = [sys.executable, str(SCRIPT), "--data-dir", str(data), "--state-dir", str(tmp_path / "state"),
           "--wiki-agent-bin", str(agent), "--result-file", str(result), "--nodes", "node-a"]
    env = {**os.environ, "CCC_SPANS_VIEW_NOW": "2026-11-30T05:00:00+09:00"}
    out = subprocess.run(cmd, capture_output=True, text=True, env=env)
    assert out.returncode == 0, out.stdout + out.stderr
    assert json.loads(out.stdout)["weeks"] == ["2026-W48", "2026-W47"]


def test_lock_blocks_a_second_run(data, tmp_path):
    standard_fleet(data)
    wt, agent, calls = fake_wiki(tmp_path)
    (tmp_path / "state").mkdir(exist_ok=True)
    with open(tmp_path / "state" / "spans-weekly.lock", "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        rc, res, line = run_cli(data, agent, tmp_path)
    assert rc == 1 and "holds the lock" in res["error"] and not calls.exists()


# ------------------------------------------------------------------ rollup


def test_rollup_candidate_points_at_the_services_page():
    text = (HERE / "spans-rollup.py").read_text(encoding="utf-8")
    assert 'SUGGESTED_PATH = "pages/services/span-rollup.md"' in text
    assert weekly.DEFAULT_PAGE == "pages/services/span-rollup.md"
