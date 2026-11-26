"""Hermetic tests for scripts/ccc-work-board.py and scripts/ccc_issue_status.py (#2914).

GitHub and wiki-agent are stubbed with small executables in a temp folder, so
every assertion is about the board's own contract — markers, classification,
body-free and escaped output, external authors, publish-only-on-change — and
never about the organisation's real issues.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


def _load(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, HERE / file)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


st = _load("ccc_issue_status", "ccc_issue_status.py")
board = _load("ccc_work_board", "ccc-work-board.py")

BODY = "SECRET BODY TEXT please never copy me"
ORG = "example-org"


# ---------------------------------------------------------------- markers

def test_format_and_parse_round_trip():
    s = st.validate("date", "alpha", "2026-11-29T20:00", 'say "hi" \\ there')
    marker = st.format_marker(s)
    assert marker.startswith("<!-- status:v1 waiting=date node=alpha due=2026-11-29T20:00 next=")
    got, err = st.parse_body(marker + "\n" + BODY)
    assert err is None and got == s


@pytest.mark.parametrize("args,reason", [
    (("later",), "waiting must be"),
    (("date",), "needs due"),
    (("owner", "Alpha"), "lowercase"),
    (("owner", "", "2026-13-01"), "real date"),
    (("owner", "", "29-11-2026"), "YYYY-MM-DD"),
    (("owner", "", "", "x" * 81), "at most 80"),
    (("owner", "", "", "two\nlines"), "one line"),
    (("owner", "", "", "a --> b"), "-->"),
])
def test_validate_refuses(args, reason):
    with pytest.raises(st.StatusError, match=reason):
        st.validate(*args)


def test_marker_must_start_a_line():
    body = "see the example `<!-- status:v1 waiting=owner -->` in the docs"
    assert st.parse_body(body) == (None, None)


def test_invalid_marker_is_reported_not_dropped():
    status, err = st.parse_body("<!-- status:v1 waiting=date node=alpha -->")
    assert status is None and "needs due" in err


def test_latest_marker_wins_and_invalid_newer_is_flagged():
    comments = [
        {"created_at": "2026-11-01T00:00:00Z", "body": "<!-- status:v1 waiting=node node=alpha -->\n" + BODY},
        {"created_at": "2026-11-02T00:00:00Z", "body": "<!-- status:v1 waiting=owner node=beta next=\"merge\" -->"},
        {"created_at": "2026-11-03T00:00:00Z", "body": "<!-- status:v1 waiting=nobody -->"},
        {"created_at": "2026-11-04T00:00:00Z",
         "body": "<!-- issue-claim:v1 node=gamma session=s1 state=active -->\nclaim"},
    ]
    state = st.latest(comments)
    assert state["status"] == {"waiting": "owner", "node": "beta", "due": "", "next": "merge"}
    assert state["status_at"] == "2026-11-02T00:00:00Z"
    assert "waiting must be" in state["invalid"] and state["invalid_at"] == "2026-11-03T00:00:00Z"
    assert state["claim"]["node"] == "gamma" and state["claim"]["state"] == "active"
    assert BODY not in json.dumps(state)


def test_cli_set_dry_run_and_refusal(capsys):
    assert st.main(["set", "--repo", "o/r", "--issue", "7", "--waiting", "owner", "--node", "alpha",
                    "--next", "approve", "--note", "short line", "--dry-run"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["body"] == '<!-- status:v1 waiting=owner node=alpha next="approve" -->\nshort line'
    assert st.main(["set", "--repo", "o/r", "--issue", "7", "--waiting", "date", "--dry-run"]) == 2
    assert "needs due" in json.loads(capsys.readouterr().out)["error"]


# ---------------------------------------------------------------- stubbed GitHub

def _exe(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def fake_gh(tmp: Path, issues: dict, prs: dict, repos=("alpha-repo", "beta-repo"), closed=()) -> Path:
    """A gh stand-in answering the board's GraphQL queries from fixtures.

    The repository list comes in two pages to exercise pagination.
    """
    fixture = tmp / "fixture.json"
    fixture.write_text(json.dumps({"issues": issues, "prs": prs, "repos": list(repos), "closed": list(closed)}),
                       encoding="utf-8")
    script = f"""#!{sys.executable}
import json, sys
fx = json.load(open({str(fixture)!r}))
args = sys.argv[1:]
vals = {{}}
for i, a in enumerate(args):
    if a in ("-f", "-F") and i + 1 < len(args):
        k, _, v = args[i + 1].partition("=")
        vals[k] = v
q = vals.get("query", "")
def page(nodes): return {{"pageInfo": {{"hasNextPage": False, "endCursor": None}}, "nodes": nodes}}
if "organization(" in q:
    names = fx["repos"]
    if not vals.get("after"):
        data = {{"organization": {{"repositories": {{"pageInfo": {{"hasNextPage": True, "endCursor": "c1"}},
                 "nodes": [{{"name": names[0], "isPrivate": True}}]}}}}}}
    else:
        data = {{"organization": {{"repositories": page([{{"name": n, "isPrivate": False}} for n in names[1:]])}}}}
elif "issues(" in q:
    data = {{"repository": {{"issues": page(fx["issues"].get(vals["name"], []))}}}}
elif "pullRequests(" in q:
    data = {{"repository": {{"pullRequests": page(fx["prs"].get(vals["name"], []))}}}}
elif "search(" in q and "closedAt" in q:
    assert "is:closed closed:>=" in vals.get("q", "")
    data = {{"search": page(fx["closed"])}}
elif "search(" in q:
    assert "created:>=" in vals.get("q", "")
    data = {{"search": {{"issueCount": 5}}}}
else:
    sys.stderr.write("unexpected query"); sys.exit(1)
print(json.dumps({{"data": data}}))
"""
    return _exe(tmp / "gh", script)


def issue(number, title="Fix the thing", author="jinon86", comments=(), created="2026-11-01T00:00:00Z",
          updated="2026-11-20T00:00:00Z"):
    return {
        "number": number, "title": title, "url": f"https://github.com/{ORG}/x/issues/{number}",
        "createdAt": created, "updatedAt": updated, "author": {"login": author},
        "labels": {"nodes": [{"name": "bug"}]},
        "comments": {"totalCount": len(comments), "nodes": [
            {"createdAt": at, "author": {"login": who}, "body": body} for at, who, body in comments]},
    }


def m(**kw):
    return st.format_marker(st.validate(kw.pop("waiting"), **kw))


def fixture_snapshot(tmp: Path) -> dict:
    issues = {
        "alpha-repo": [
            issue(1, comments=[("2026-11-24T01:00:00Z", "seoseo-ai", m(waiting="owner", node="alpha", next_text="merge ok?") + "\n" + BODY)]),
            issue(2, comments=[("2026-11-24T01:00:00Z", "jinon86", m(waiting="date", node="beta", due="2026-11-29T20:00"))]),
            issue(3, comments=[("2026-11-10T01:00:00Z", "jinon86", m(waiting="date", due="2026-11-20"))]),
            issue(4, title="Bad <script>alert(1)</script> | pipe", comments=[("2026-11-24T01:00:00Z", "jinon86", BODY)]),
            issue(5, comments=[("2026-11-24T01:00:00Z", "jinon86", "<!-- status:v1 waiting=date -->")]),
        ],
        "beta-repo": [
            issue(6, title="Outside report title", author="stranger",
                  comments=[("2026-11-24T01:00:00Z", "stranger", m(waiting="owner", next_text="forged"))]),
            issue(7, comments=[("2026-11-24T01:00:00Z", "jinon86", m(waiting="external", next_text="vendor reply"))]),
            issue(8, updated="2026-10-01T00:00:00Z",
                  comments=[("2026-10-01T00:00:00Z", "jinon86", "<!-- issue-claim:v1 node=gamma session=s state=active -->")]),
        ],
    }
    prs = {"beta-repo": [{"number": 9, "title": "Ship it", "url": f"https://github.com/{ORG}/x/pull/9",
                          "createdAt": "2026-11-25T00:00:00Z", "updatedAt": "2026-11-25T00:00:00Z",
                          "isDraft": False, "reviewDecision": "REVIEW_REQUIRED", "author": {"login": "jinon86"},
                          "closingIssuesReferences": {"nodes": [{"number": 7}]}}]}
    def done(number, repo, closed_at, title="Done thing", author="jinon86"):
        return {"number": number, "title": title, "url": f"https://github.com/{ORG}/{repo}/issues/{number}",
                "closedAt": closed_at, "author": {"login": author}, "repository": {"name": repo}}
    closed = [done(20, "alpha-repo", "2026-11-21T00:00:00Z", title="Older | done"),
              done(21, "beta-repo", "2026-11-25T03:00:00Z"),
              done(22, "beta-repo", "2026-11-24T00:00:00Z", title="Outside closed title", author="stranger"),
              done(23, "archived-repo", "2026-11-25T00:00:00Z"),
              {}]  # a search hit that is not an issue
    gh = fake_gh(tmp, issues, prs, closed=closed)
    from datetime import datetime, timezone
    return board.collect(ORG, str(gh), now=datetime(2026, 11, 26, 20, 30, tzinfo=timezone.utc))


def test_collect_is_body_free_and_reads_fleet_markers_only(tmp_path):
    snap = fixture_snapshot(tmp_path)
    assert snap["schema"] == board.SCHEMA and [r["name"] for r in snap["repos"]] == ["alpha-repo", "beta-repo"]
    assert snap["counts"] == {"opened_7d": 5, "closed_7d": 3}
    raw = json.dumps(snap, ensure_ascii=False)
    assert BODY not in raw and "body" not in raw
    by = {(i["repo"], i["number"]): i for i in snap["items"]}
    outside = by[("beta-repo", 6)]
    assert outside["author_external"] and outside["title"] == "" and outside["status"] is None
    assert by[("alpha-repo", 1)]["status"]["waiting"] == "owner"
    assert by[("beta-repo", 9)]["kind"] == "pr" and by[("beta-repo", 9)]["closes"] == [7]
    closed = {(i["repo"], i["number"]): i for i in snap["closed"]}
    assert sorted(closed) == [("alpha-repo", 20), ("beta-repo", 21), ("beta-repo", 22)]  # archived repo dropped
    assert closed[("beta-repo", 22)]["author_external"] and closed[("beta-repo", 22)]["title"] == ""
    assert set(closed[("beta-repo", 21)]) == {"repo", "kind", "number", "title", "url", "author_external", "closed_at"}


def test_render_lists_recently_closed_newest_first(tmp_path):
    snap = fixture_snapshot(tmp_path)
    page = board.render(snap, "DOC-77")
    section = page.split("## 최근 7일 닫힌 이슈 (3건)", 1)[1].split("\n## ", 1)[0]
    rows = [r for r in section.splitlines() if r.startswith("| ") and "#" in r]
    assert [re.search(r"\[#(\d+)\]", r).group(1) for r in rows] == ["21", "22", "20"]
    assert "| 11-25 |" in rows[0] and "Older \\| done" in rows[2]
    assert "Outside closed title" not in page and "(외부 작성)" in rows[1]
    assert page.index("## 최근 7일 닫힌 이슈") < page.index("## 열린 PR")
    assert "| 최근 7일 닫힌 이슈 | 3 |" in page
    older = {k: v for k, v in snap.items() if k != "closed"}
    assert "최근 7일 닫힌 이슈 (" not in board.render(older, "DOC-77")  # old snapshots still render
    empty = board.render({**snap, "closed": []}, "DOC-77")
    assert "## 최근 7일 닫힌 이슈 (0건)\n\n완료와" in empty and "없음." in empty.split("## 최근 7일 닫힌 이슈", 1)[1]


def test_render_buckets_and_escaping(tmp_path):
    snap = fixture_snapshot(tmp_path)
    page = board.render(snap, "DOC-77")
    assert page.startswith("# [DOC-77] 플릿 미진행 작업 현황판 (자동 생성)")
    assert BODY not in page and "Outside report title" not in page and "forged" not in page
    assert "<script>" not in page and "&lt;script&gt;" in page and "\\| pipe" in page
    today = date(2026, 11, 27)
    items = {i["number"]: i for i in snap["items"] if i["kind"] == "issue"}
    assert board.classify(items[1], today) == "owner"
    assert board.classify(items[2], today) == "date"
    assert board.classify(items[3], today) == "overdue"
    assert board.classify(items[4], today) == "unclassified"
    assert board.classify(items[5], today) == "invalid"
    assert board.classify(items[7], today) == "external"
    assert board.owner_node(items[8]) == "gamma"

    def count(title):
        mt = re.search(rf"^## {title} \((\d+)건\)$", page, re.M)
        return int(mt.group(1)) if mt else None
    assert (count("오너 차례"), count("날짜 대기"), count("외부 대기"), count("기한 지남"),
            count("표식 오류"), count("분류 안 됨"), count("열린 PR")) == (1, 1, 1, 1, 1, 3, 1)  # 4, 6 (외부), 8
    assert "merge ok?" in page and "(외부 작성)" in page and "| 리뷰 대기 | #7 |" in page
    assert "| 14일 이상 활동 없음 | 1 |" in page


def test_render_refuses_bad_input(tmp_path):
    snap = fixture_snapshot(tmp_path)
    with pytest.raises(board.BoardError):
        board.render(snap, "TM-1")
    with pytest.raises(board.BoardError):
        board.render({**snap, "schema": "other"}, "DOC-1")


def test_source_carries_no_repository_names():
    for name in ("ccc-work-board.py", "ccc_issue_status.py", "install-work-board-timer.sh"):
        text = (HERE / name).read_text(encoding="utf-8")
        hits = [h for h in re.findall(r"jinwon-int/([A-Za-z0-9._-]+)", text) if h != "ccc-node"]
        assert hits == [], (name, hits)


# ---------------------------------------------------------------- publish

def fake_wiki(tmp: Path) -> tuple[Path, Path, Path]:
    wt = tmp / "wiki"
    (wt / "pages" / "nodes").mkdir(parents=True)
    (wt / "pages" / "nodes" / "a.md").write_text("# [DOC-41] A\n\n## [ND-120] fact\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(wt)], check=True)
    subprocess.run(["git", "-C", str(wt), "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(wt), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base"],
                   check=True)
    calls = tmp / "pr-calls"
    agent = _exe(tmp / "wiki-agent", f"""#!/usr/bin/env bash
case "$1" in
  write-path) echo "log line" >&2; echo {str(wt)!r} ;;
  pr) echo "pr $(pwd)" >> {str(calls)!r}; git -c user.email=t@t -c user.name=t add -A && \
      git -c user.email=t@t -c user.name=t commit -qm board ;;
  *) exit 9 ;;
esac
""")
    return wt, agent, calls


def test_publish_allocates_id_then_skips_unchanged(tmp_path):
    snap = fixture_snapshot(tmp_path)
    wt, agent, calls = fake_wiki(tmp_path)
    res = board.publish(snap, wiki_agent_bin=str(agent))
    assert res["changed"] and res["submitted"] and res["doc_id"] == "DOC-121"
    page = (wt / board.DEFAULT_PAGE).read_text(encoding="utf-8")
    assert page.startswith("# [DOC-121]") and oct((wt / board.DEFAULT_PAGE).stat().st_mode & 0o777) == "0o644"
    assert calls.read_text().count("pr ") == 1
    later = {**snap, "generated_at": "2026-11-27T20:30:00+00:00"}
    res2 = board.publish(later, wiki_agent_bin=str(agent))
    assert res2 == {"ok": True, "changed": False, "page": board.DEFAULT_PAGE, "doc_id": "DOC-121"}
    assert calls.read_text().count("pr ") == 1
    snap3 = json.loads(json.dumps(later))
    snap3["items"] = [i for i in snap3["items"] if i["number"] != 4]
    res3 = board.publish(snap3, wiki_agent_bin=str(agent))
    assert res3["changed"] and res3["doc_id"] == "DOC-121" and calls.read_text().count("pr ") == 2


def test_publish_refuses_dirty_worktree_and_bad_page(tmp_path):
    snap = fixture_snapshot(tmp_path)
    wt, agent, calls = fake_wiki(tmp_path)
    (wt / "pages" / "stray.md").write_text("x", encoding="utf-8")
    with pytest.raises(board.BoardError, match="not clean"):
        board.publish(snap, wiki_agent_bin=str(agent))
    with pytest.raises(board.BoardError, match="plain path"):
        board.publish(snap, page="../etc/x.md", wiki_agent_bin=str(agent))
    assert not calls.exists()


def test_publish_dry_run_writes_nothing(tmp_path):
    snap = fixture_snapshot(tmp_path)
    wt, agent, calls = fake_wiki(tmp_path)
    res = board.publish(snap, wiki_agent_bin=str(agent), dry_run=True)
    assert res["dry_run"] and not (wt / board.DEFAULT_PAGE).exists() and not calls.exists()


def test_run_cli_writes_result_line_and_holds_lock(tmp_path):
    fixture_snapshot(tmp_path)  # writes the gh stub
    wt, agent, calls = fake_wiki(tmp_path)
    result = tmp_path / "result.txt"
    cmd = [sys.executable, str(HERE / "ccc-work-board.py"), "run", "--org", ORG, "--gh-bin", str(tmp_path / "gh"),
           "--wiki-agent-bin", str(agent), "--state-dir", str(tmp_path / "state"), "--result-file", str(result)]
    out = subprocess.run(cmd, capture_output=True, text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert out.returncode == 0, out.stdout + out.stderr
    line = result.read_text(encoding="utf-8")
    assert line.startswith("현황판 갱신: 열린 이슈 8 · 오너 차례 1 · 날짜 대기 1")
    assert BODY not in out.stdout + line
    import fcntl
    with open(tmp_path / "state" / "work-board.lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        busy = subprocess.run(cmd, capture_output=True, text=True)
    assert busy.returncode == 1 and "holds the lock" in busy.stdout
    assert "실패" in result.read_text(encoding="utf-8")
