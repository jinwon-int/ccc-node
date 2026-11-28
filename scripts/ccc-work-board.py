#!/usr/bin/env python3
"""Fleet work board (#2914): open issues and PRs of an organisation -> one Wiki page.

Read-only towards GitHub. Runs once a day on the hub node (see
scripts/install-work-board-timer.sh) and rewrites one auto-generated Wiki page
through the normal PR-first path (``wiki-agent write-path`` -> page -> ``wiki-agent pr``).

  ccc-work-board.py collect --org ORG --out SNAPSHOT.json
  ccc-work-board.py render  --snapshot SNAPSHOT.json [--doc-id DOC-N] [--out PAGE.md]
  ccc-work-board.py publish --snapshot SNAPSHOT.json [--page pages/current/work-board.md] [--dry-run]
  ccc-work-board.py run     --org ORG [--page …] [--dry-run] [--result-file P]

What goes onto the page: repository name, number, title, labels, dates, the
fields of the latest ``status:v1`` marker and of the latest ``issue-claim:v1``
marker; for issues closed in the last 7 days only repository, number, title and
closing date. Comment bodies are read only to find those markers and are never
stored or rendered. Issues opened by authors outside the fleet are listed by
number only (no title), and markers are read only from fleet authors' comments.

The repository list is read from the API at run time; no repository name is
written in this file (it lives in a public repository).
"""

from __future__ import annotations

import argparse
import fcntl
import html
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

HERE = Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location("ccc_issue_status", HERE / "ccc_issue_status.py")
assert _SPEC is not None and _SPEC.loader is not None
issue_status = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = issue_status  # dataclasses look the module up while building Status
_SPEC.loader.exec_module(issue_status)

SCHEMA = "ccc.work-board.v1"
KST = timezone(timedelta(hours=9))
DEFAULT_PAGE = "pages/current/work-board.md"
DEFAULT_FLEET_AUTHORS = ("jinon86", "seoseo-ai")
STALE_DAYS = 14
COMMENTS_PER_ITEM = 40
BUCKETS = ("owner", "date", "node", "external", "refresh", "overdue", "invalid", "standing", "outside",
           "unclassified")
BUCKET_TITLE = {
    "owner": "오너 차례", "date": "날짜 대기", "node": "노드 작업", "external": "외부 대기",
    "refresh": "표식 갱신 필요", "overdue": "기한 지남", "invalid": "표식 오류",
    "standing": "상시 추적", "outside": "외부 작성", "unclassified": "분류 안 됨",
}
# Buckets that come from a status marker (the per-repository "표식 있음" count).
MARKED_BUCKETS = ("owner", "date", "node", "external", "refresh", "overdue")
# A standing tracker (an auto-updated report or to-do issue that is never "done")
# carries this label; without a marker it is listed apart instead of as unclassified.
STANDING_LABEL = "board:standing"
GENERATED_LINE = "> **에이전트 다이제스트** — status: `active` · 자동 생성: "


class BoardError(RuntimeError):
    pass


# ---------------------------------------------------------------------------- GitHub

_REPOS_Q = """
query($org:String!, $after:String) {
  organization(login:$org) {
    repositories(first:100, after:$after, isArchived:false, orderBy:{field:NAME, direction:ASC}) {
      pageInfo { hasNextPage endCursor }
      nodes { name isPrivate }
    }
  }
}"""

_ISSUES_Q = """
query($owner:String!, $name:String!, $after:String) {
  repository(owner:$owner, name:$name) {
    issues(states:OPEN, first:50, after:$after, orderBy:{field:CREATED_AT, direction:ASC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title url createdAt updatedAt
        author { login }
        labels(first:10) { nodes { name } }
        comments(last:%d) { totalCount nodes { createdAt author { login } body } }
      }
    }
  }
}""" % COMMENTS_PER_ITEM

_PRS_Q = """
query($owner:String!, $name:String!, $after:String) {
  repository(owner:$owner, name:$name) {
    pullRequests(states:OPEN, first:50, after:$after, orderBy:{field:CREATED_AT, direction:ASC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title url createdAt updatedAt isDraft reviewDecision
        author { login }
        closingIssuesReferences(first:5) { nodes { number } }
      }
    }
  }
}"""

_COUNT_Q = """
query($q:String!) { search(query:$q, type:ISSUE, first:1) { issueCount } }"""

_CLOSED_Q = """
query($q:String!, $after:String) {
  search(query:$q, type:ISSUE, first:100, after:$after) {
    pageInfo { hasNextPage endCursor }
    nodes { ... on Issue { number title url closedAt author { login } repository { name } } }
  }
}"""


def gh_graphql(gh_bin: str, query: str, variables: dict) -> dict:
    args = [gh_bin, "api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        if value is None:
            continue
        args += ["-F" if isinstance(value, int) else "-f", f"{key}={value}"]
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BoardError(f"gh graphql failed: {type(exc).__name__}") from None
    if result.returncode != 0:
        raise BoardError(f"gh graphql failed rc={result.returncode}: {result.stderr.strip()[:200]}")
    try:
        data = json.loads(result.stdout)
    except ValueError:
        raise BoardError("gh graphql returned non-JSON") from None
    if data.get("errors"):
        raise BoardError(f"graphql errors: {str(data['errors'])[:200]}")
    return data.get("data") or {}


def _paged(gh_bin: str, query: str, variables: dict, path: Sequence[str]) -> list:
    out, after = [], None
    for _ in range(200):
        data = gh_graphql(gh_bin, query, {**variables, "after": after})
        node: Any = data
        for key in path:
            node = (node or {}).get(key)
        if node is None:
            raise BoardError(f"graphql: missing {'.'.join(path)}")
        out.extend(node.get("nodes") or [])
        page = node.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            return out
        after = page.get("endCursor")
    raise BoardError("graphql: too many pages")


def is_fleet_author(login: str, fleet: Iterable[str]) -> bool:
    login = (login or "").strip()
    if not login:
        return False
    return login in set(fleet) or login.endswith("[bot]") or login in ("github-actions", "dependabot", "renovate")


def _item_from_issue(repo: str, node: dict, fleet: Sequence[str]) -> dict:
    author = ((node.get("author") or {}).get("login")) or ""
    external = not is_fleet_author(author, fleet)
    comments = (node.get("comments") or {}).get("nodes") or []
    fleet_comments = [
        {"created_at": c.get("createdAt") or "", "body": c.get("body") or ""}
        for c in comments if is_fleet_author(((c.get("author") or {}).get("login")) or "", fleet)
    ]
    state = issue_status.latest(fleet_comments)
    last_comment = max((c.get("createdAt") or "" for c in comments), default="")
    last_fleet_comment = max((c["created_at"] for c in fleet_comments), default="")
    return {
        "repo": repo, "kind": "issue", "number": int(node["number"]),
        "title": "" if external else (node.get("title") or ""),
        "url": node.get("url") or "", "author_external": external,
        "created_at": node.get("createdAt") or "", "updated_at": node.get("updatedAt") or "",
        "last_comment_at": last_comment, "last_fleet_comment_at": last_fleet_comment,
        "labels": [lb.get("name") or "" for lb in ((node.get("labels") or {}).get("nodes") or [])],
        "comments": int((node.get("comments") or {}).get("totalCount") or 0),
        **state,
    }


def _item_from_pr(repo: str, node: dict, fleet: Sequence[str]) -> dict:
    author = ((node.get("author") or {}).get("login")) or ""
    external = not is_fleet_author(author, fleet)
    return {
        "repo": repo, "kind": "pr", "number": int(node["number"]),
        "title": "" if external else (node.get("title") or ""),
        "url": node.get("url") or "", "author_external": external,
        "created_at": node.get("createdAt") or "", "updated_at": node.get("updatedAt") or "",
        "draft": bool(node.get("isDraft")), "review": node.get("reviewDecision") or "",
        "closes": sorted(int(n.get("number")) for n in ((node.get("closingIssuesReferences") or {}).get("nodes") or [])),
    }


def _item_from_closed(node: dict, fleet: Sequence[str]) -> dict:
    author = ((node.get("author") or {}).get("login")) or ""
    external = not is_fleet_author(author, fleet)
    return {
        "repo": ((node.get("repository") or {}).get("name")) or "", "kind": "closed",
        "number": int(node["number"]), "title": "" if external else (node.get("title") or ""),
        "url": node.get("url") or "", "author_external": external, "closed_at": node.get("closedAt") or "",
    }


def collect_closed(org: str, gh_bin: str, since: str, repo_names: Iterable[str],
                   fleet: Sequence[str] = DEFAULT_FLEET_AUTHORS) -> list:
    """Issues closed on or after ``since`` (YYYY-MM-DD) in the listed repositories.

    Same fields as an open issue minus everything marker-related: no comment is
    read. Issues in repositories outside ``repo_names`` (archived ones) are dropped.
    """
    names = set(repo_names)
    q = f"org:{org} is:issue is:closed closed:>={since}"
    out = []
    for node in _paged(gh_bin, _CLOSED_Q, {"q": q}, ("search",)):
        if not node or "number" not in node:
            continue
        item = _item_from_closed(node, fleet)
        if item["repo"] in names:
            out.append(item)
    return out


def collect(org: str, gh_bin: str = "gh", fleet: Sequence[str] = DEFAULT_FLEET_AUTHORS,
            now: Optional[datetime] = None) -> dict:
    now = now or datetime.now(timezone.utc)
    repos = _paged(gh_bin, _REPOS_Q, {"org": org}, ("organization", "repositories"))
    items: list = []
    for repo in repos:
        name = repo.get("name") or ""
        if not re.match(r"^[A-Za-z0-9._-]+$", name):
            continue
        for node in _paged(gh_bin, _ISSUES_Q, {"owner": org, "name": name}, ("repository", "issues")):
            items.append(_item_from_issue(name, node, fleet))
        for node in _paged(gh_bin, _PRS_Q, {"owner": org, "name": name}, ("repository", "pullRequests")):
            items.append(_item_from_pr(name, node, fleet))
    since = (now - timedelta(days=7)).date().isoformat()
    data = gh_graphql(gh_bin, _COUNT_Q, {"q": f"org:{org} is:issue created:>={since}"})
    closed = collect_closed(org, gh_bin, since, [r.get("name") or "" for r in repos], fleet)
    counts = {"opened_7d": int(((data.get("search") or {}).get("issueCount")) or 0), "closed_7d": len(closed)}
    return {
        "schema": SCHEMA, "org": org, "generated_at": now.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "repos": [{"name": r.get("name"), "private": bool(r.get("isPrivate"))} for r in repos],
        "items": items, "closed": closed, "counts": counts,
    }


# ---------------------------------------------------------------------------- node schedules (stage 3)

SCHEDULES_SCHEMA = "ccc.work-board.schedules.v1"
COLLECTOR = HERE / "ccc-node-schedules.py"
NODE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")


def _node_list(value: str) -> list:
    nodes = [n for n in re.split(r"[\s,]+", value or "") if n]
    bad = [n for n in nodes if not NODE_RE.match(n)]
    if bad:
        raise BoardError("node names must be lowercase [a-z0-9._-]")
    return nodes


def collect_schedules(nodes: Sequence[str], self_node: str = "", ssh_bin: str = "ssh",
                      python_bin: str = sys.executable, timeout: int = 90,
                      now: Optional[datetime] = None) -> dict:
    """Run the read-only collector on every node and keep only its JSON.

    The collector source is piped over SSH (``python3 -``), so the nodes need no
    install and run the hub's version. A node that cannot be reached, or answers
    with anything that is not a collector document, becomes an ``error`` row;
    its stderr is never kept."""
    src = COLLECTOR.read_text(encoding="utf-8")
    out = []
    for node in nodes:
        if node == self_node:
            argv = [python_bin, "-", "--node", node]
        else:
            argv = [ssh_bin, "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", node, f"python3 - --node {node}"]
        try:
            proc = subprocess.run(argv, input=src, capture_output=True, text=True, timeout=timeout)
            doc = json.loads(proc.stdout) if proc.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired, ValueError):
            doc = None
        if not isinstance(doc, dict) or doc.get("schema") != "ccc.node-schedules.v1" or not isinstance(doc.get("items"), list):
            out.append({"node": node, "error": "수집 실패", "items": []})
            continue
        doc["node"] = node  # the hub's name for it, not what the node calls itself
        out.append(doc)
    now = now or datetime.now(timezone.utc)
    return {"schema": SCHEDULES_SCHEMA, "collected_at": now.isoformat(timespec="seconds"), "nodes": out}


# ---------------------------------------------------------------------------- classify / render

def _kst(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(KST)
    except ValueError:
        return None


def _due_date(due: str) -> Optional[date]:
    try:
        return datetime.strptime(due[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def last_activity(item: dict) -> str:
    return max(item.get("updated_at") or "", item.get("last_comment_at") or "")


def owner_node(item: dict) -> str:
    status = item.get("status") or {}
    if status.get("node"):
        return status["node"]
    claim = item.get("claim") or {}
    if claim.get("node") and claim.get("state") in ("active", "takeover"):
        return claim["node"]
    return ""


def _due_at(due: str) -> Optional[datetime]:
    """``due`` as a KST instant; a bare date means the start of that day."""
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(due or "", fmt).replace(tzinfo=KST)
        except ValueError:
            continue
    return None


def result_after_due(item: dict, now: datetime) -> bool:
    """A dated wait whose time has come and whose issue got a fleet comment at or
    after ``due`` that is newer than the marker: the scheduled step most likely
    reported its result, but nobody moved the marker on."""
    status = item.get("status") or {}
    if status.get("waiting") != "date":
        return False
    due_at = _due_at(status.get("due") or "")
    later = _kst(item.get("last_fleet_comment_at") or "")
    marked = _kst(item.get("status_at") or "")
    if due_at is None or later is None or due_at > now:
        return False
    return later >= due_at and (marked is None or later > marked)


def classify(item: dict, today: date, now: Optional[datetime] = None) -> str:
    status = item.get("status")
    if item.get("invalid") and (not status or (item.get("invalid_at") or "") > (item.get("status_at") or "")):
        return "invalid"
    if not status:
        if STANDING_LABEL in (item.get("labels") or []):
            return "standing"
        if item.get("author_external"):
            return "outside"
        return "unclassified"
    if now is not None and result_after_due(item, now):
        return "refresh"
    due = _due_date(status.get("due") or "")
    if due is not None and due < today - timedelta(days=1):
        return "overdue"
    return status.get("waiting") or "unclassified"


def _cell(text: str, limit: int = 90) -> str:
    text = " ".join(str(text or "").split())
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return html.escape(text, quote=False).replace("|", "\\|")


def _ref(item: dict) -> str:
    label = f"#{item['number']}"
    title = "(외부 작성)" if item.get("author_external") else item.get("title") or ""
    url = item.get("url") or ""
    if not url.startswith("https://github.com/"):
        return _cell(f"{label} {title}")
    return f"[{label}]({url}) {_cell(title, 70)}"


def _day(ts: str) -> str:
    dt = _kst(ts)
    return dt.strftime("%m-%d") if dt else "—"


def _status_row(item: dict) -> str:
    s = item.get("status") or {}
    return (f"| {_cell(item['repo'])} | {_ref(item)} | {_cell(s.get('next') or '—', 80)} | "
            f"{_cell(owner_node(item) or '—')} | {_cell(s.get('due') or '—')} | {_day(item.get('status_at') or '')} |")


def _invalid_row(item: dict) -> str:
    return (f"| {_cell(item['repo'])} | {_ref(item)} | {_cell(item.get('invalid') or '')} | "
            f"{_day(item.get('invalid_at') or '')} |")


def _unclassified_row(item: dict) -> str:
    return (f"| {_cell(item['repo'])} | {_ref(item)} | {_cell(owner_node(item) or '—')} | "
            f"{_day(last_activity(item))} | {_day(item.get('created_at') or '')} |")


def _refresh_row(item: dict) -> str:
    s = item.get("status") or {}
    return (f"| {_cell(item['repo'])} | {_ref(item)} | {_cell(s.get('next') or '—', 80)} | "
            f"{_cell(owner_node(item) or '—')} | {_cell(s.get('due') or '—')} | "
            f"{_day(item.get('last_fleet_comment_at') or '')} |")


def _plain_row(item: dict) -> str:
    return (f"| {_cell(item['repo'])} | {_ref(item)} | {_day(last_activity(item))} | "
            f"{_day(item.get('created_at') or '')} |")


_STATUS_HEADER = "| 저장소 | 이슈 | 다음 행동 | 노드 | 기한 | 표식 |"


def _by_marker(item: dict) -> tuple:
    return (item.get("status_at") or "", item["repo"], item["number"])


def _by_due(item: dict) -> tuple:
    return ((item.get("status") or {}).get("due") or "9999", item["repo"], item["number"])


_SECTIONS = (
    ("owner", _STATUS_HEADER, _by_marker, _status_row),
    ("date", _STATUS_HEADER, _by_due, _status_row),
    ("node", _STATUS_HEADER, _by_marker, _status_row),
    ("external", _STATUS_HEADER, _by_marker, _status_row),
    ("refresh", "| 저장소 | 이슈 | 표식의 다음 행동 | 노드 | 기한 | 기한 뒤 코멘트 |", _by_due, _refresh_row),
    ("overdue", _STATUS_HEADER, _by_due, _status_row),
    ("invalid", "| 저장소 | 이슈 | 오류 | 표식 |", lambda i: (i["repo"], i["number"]), _invalid_row),
    ("standing", "| 저장소 | 이슈 | 마지막 활동 | 생성 |", lambda i: (i["repo"], i["number"]), _plain_row),
    ("outside", "| 저장소 | 이슈 | 마지막 활동 | 생성 |", lambda i: (i["repo"], i["number"]), _plain_row),
    ("unclassified", "| 저장소 | 이슈 | 선점 노드 | 마지막 활동 | 생성 |",
     lambda i: (last_activity(i), i["repo"], i["number"]), _unclassified_row),
)


def _render_head(out: list, doc_id: str, org: str, gen: datetime) -> None:
    out += [
        f"# [{doc_id}] 플릿 미진행 작업 현황판 (자동 생성)",
        "",
        f"{GENERATED_LINE}{gen.strftime('%Y-%m-%d %H:%M')} KST (매일)",
        f"> - 이 페이지: `{org}` 조직의 보관되지 않은 저장소 전체의 열린 이슈·PR과 최근 7일 닫힌 이슈를 매일 다시 모은 현황판이다. **손으로 고치지 말 것** — 다음 실행 때 덮어쓴다.",
        "> - 분류: 이슈 코멘트의 가장 최근 `status:v1` 표식으로 오너 차례 · 날짜 대기 · 노드 작업 · 외부 대기를 나눈다. 기한 뒤 결과 코멘트가 달렸는데 표식이 그대로면 「표식 갱신 필요」, 기한이 하루 넘게 지났으면 「기한 지남」이다. 표식이 없으면 `board:standing` 라벨은 「상시 추적」, 외부 작성자의 이슈는 「외부 작성」, 나머지는 「분류 안 됨」이다.",
        "> - 근거: GitHub API — 제목 · 번호 · 날짜 · 라벨 · 표식 필드만 싣고 코멘트 본문은 싣지 않는다. 외부 작성자의 이슈는 번호만 싣는다. 생성기: ccc-node `scripts/ccc-work-board.py`(ccc-node#2914).",
        "> - stale 주의: 생성 시각의 스냅샷이다. 표식은 노드가 보고할 때 남긴 값이라 실제 진행보다 늦을 수 있다. 이슈가 없는 일은 여기에 오르지 않는다.",
        "",
    ]


def _render_summary(out: list, issues: list, prs: list, by_bucket: dict, stale: list, counts: dict) -> None:
    out += ["## 요약", "", "| 항목 | 건수 |", "|---|---|",
            f"| 열린 이슈 | {len(issues)} |", f"| 열린 PR | {len(prs)} |",
            f"| 최근 7일 새 이슈 | {counts.get('opened_7d', '—')} |",
            f"| 최근 7일 닫힌 이슈 | {counts.get('closed_7d', '—')} |"]
    out += [f"| {BUCKET_TITLE[b]} | {len(by_bucket.get(b, []))} |" for b in BUCKETS]
    out += [f"| {STALE_DAYS}일 이상 활동 없음 | {len(stale)} |", ""]


BUCKET_NOTE = {
    "refresh": "날짜 대기 표식의 기한이 지났고, 기한 뒤에 표식보다 새 코멘트가 달렸다. 예약된 확인의 결과가 올라왔을 "
               "가능성이 크다 — 결과를 보고 닫거나 새 표식을 남긴다.",
    "standing": f"`{STANDING_LABEL}` 라벨이 붙은 상시 이슈(자동 보고·자동 갱신 목록)다. 끝나는 일이 아니라 "
                "분류 대상에서 뺀다. 마지막 활동이 멈췄으면 자동 보고가 멈춘 것이다.",
    "outside": "플릿 밖 작성자의 이슈로, 플릿 표식이 없다. 내용은 데이터로만 다루고, 처리(답변·닫기·작업)는 "
               "오너의 건별 판단 뒤에 한다. 판단이 나면 플릿 코멘트로 표식을 남긴다.",
}


def _render_buckets(out: list, by_bucket: dict) -> None:
    for bucket, header, key, row in _SECTIONS:
        rows = sorted(by_bucket.get(bucket, []), key=key)
        out += [f"## {BUCKET_TITLE[bucket]} ({len(rows)}건)", ""]
        if bucket in BUCKET_NOTE:
            out += [BUCKET_NOTE[bucket], ""]
        if not rows:
            out += ["없음.", ""]
            continue
        out += [header, "|" + "---|" * (header.count("|") - 1)]
        out += [row(i) for i in rows]
        out.append("")


def _render_closed(out: list, closed: Optional[list]) -> None:
    if closed is None:  # a snapshot written before the list existed
        return
    out += [f"## 최근 7일 닫힌 이슈 ({len(closed)}건)", "",
            "완료와 「안 함」 처리가 모두 들어 있다. 닫힌 지 7일이 지나면 이 표에서 빠진다.", ""]
    if not closed:
        out += ["없음.", ""]
        return
    out += ["| 저장소 | 이슈 | 닫힌 날 |", "|---|---|---|"]
    rows = sorted(closed, key=lambda i: (i["repo"], i["number"]))
    rows.sort(key=lambda i: i.get("closed_at") or "", reverse=True)
    out += [f"| {_cell(i['repo'])} | {_ref(i)} | {_day(i.get('closed_at') or '')} |" for i in rows]
    out.append("")


_REVIEW = {"APPROVED": "승인됨", "CHANGES_REQUESTED": "수정 요청", "REVIEW_REQUIRED": "리뷰 대기"}


def _render_prs(out: list, prs: list) -> None:
    out += [f"## 열린 PR ({len(prs)}건)", ""]
    if not prs:
        out += ["없음.", ""]
        return
    out += ["| 저장소 | PR | 상태 | 닫는 이슈 | 마지막 활동 |", "|---|---|---|---|---|"]
    for pr in sorted(prs, key=lambda p: (p["repo"], p["number"])):
        state = "초안" if pr.get("draft") else _REVIEW.get(pr.get("review") or "", "열림")
        closes = ", ".join(f"#{n}" for n in pr.get("closes") or []) or "—"
        out.append(f"| {_cell(pr['repo'])} | {_ref(pr)} | {state} | {closes} | {_day(pr.get('updated_at') or '')} |")
    out.append("")


def _render_nodes(out: list, issues: list) -> None:
    node_counts: dict = defaultdict(Counter)
    for item in issues:
        node_counts[owner_node(item) or "(없음)"][item["_bucket"]] += 1
    out += ["## 노드별", "", "노드는 표식의 `node`, 없으면 활성 선점 표시의 노드다.", "",
            "| 노드 | " + " | ".join(BUCKET_TITLE[b] for b in BUCKETS) + " | 합계 |",
            "|---|" + "---|" * (len(BUCKETS) + 1)]
    for node in sorted(node_counts, key=lambda n: (n == "(없음)", n)):
        c = node_counts[node]
        out.append(f"| {_cell(node)} | " + " | ".join(str(c.get(b, 0)) for b in BUCKETS) + f" | {sum(c.values())} |")
    out.append("")


def _render_repos(out: list, issues: list, prs: list, stale: list) -> None:
    rows: dict = defaultdict(Counter)
    for item in issues:
        rows[item["repo"]]["issues"] += 1
        rows[item["repo"]]["classified"] += item["_bucket"] in MARKED_BUCKETS
    for pr in prs:
        rows[pr["repo"]]["prs"] += 1
    for item in stale:
        rows[item["repo"]]["stale"] += 1
    out += ["## 저장소별", "", f"| 저장소 | 열린 이슈 | 열린 PR | 표식 있음 | {STALE_DAYS}일 이상 활동 없음 |",
            "|---|---|---|---|---|"]
    for repo in sorted(rows, key=lambda r: (-rows[r]["issues"], r)):
        c = rows[repo]
        out.append(f"| {_cell(repo)} | {c['issues']} | {c['prs']} | {c['classified']} | {c['stale']} |")
    out.append("")


_SCHED_AT = re.compile(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}):(\d{2})")


def _sched_at(value: str) -> Optional[datetime]:
    """A detached-job time like ``2026-11-30 11:40:00 Asia/Seoul`` (fleet time is KST)
    or an ISO instant from systemd."""
    if not value:
        return None
    if re.search(r"[+-]\d{2}:\d{2}$|Z$", value):
        return _kst(value)
    mt = _SCHED_AT.match(value)
    if not mt:
        return None
    return datetime.strptime(f"{mt.group(1)} {mt.group(2)}:{mt.group(3)}", "%Y-%m-%d %H:%M").replace(tzinfo=KST)


def _when(item: dict) -> str:
    at = _sched_at(item.get("next_at") or "")
    if at is not None:
        return at.astimezone(KST).strftime("%m-%d %H:%M")
    if (item.get("source") or "").startswith("systemd"):
        return "다시 안 돎"  # an elapsed one-shot timer: nothing scheduled any more
    return _cell(item.get("schedule") or "—", 24)


_SOURCE_TITLE = {"systemd": "타이머", "systemd-user": "타이머(사용자)", "cron": "cron", "detached-job": "예약 작업",
                 "agent-cron": "agent-cron"}


def _render_schedules(out: list, schedules: Optional[dict], issues: list, closed: Optional[list],
                      gen: datetime) -> None:
    if not schedules:  # stage 3 not wired on this hub, or an older snapshot
        return
    nodes = schedules.get("nodes") or []
    open_keys = {(i["repo"], i["number"]): i for i in issues}
    closed_keys = {(i["repo"], i["number"]): i for i in closed or []}
    out += ["## 예약 점검", "",
            "노드마다 읽기 전용으로 모은 예약(타이머 · cron · 예약 작업 · agent-cron)이다. 이름 · 일정 · 한 줄 설명만 "
            "모으고 명령줄 · 환경 · 프롬프트는 모으지 않는다. 「일시」 타이머는 재부팅하면 사라진다(#2827).", "",
            "| 노드 | 타이머 | 일시 타이머 | cron | 예약 작업 | 지난 예약 작업(미확인) | agent-cron(켜짐) | 수집 |",
            "|---|---|---|---|---|---|---|---|"]
    linked, stale_jobs = [], []
    for doc in sorted(nodes, key=lambda d: d.get("node") or ""):
        items = doc.get("items") or []
        c = Counter(i.get("source") for i in items)
        timers = c["systemd"] + c["systemd-user"]
        transient = sum(1 for i in items if i.get("source", "").startswith("systemd") and i.get("persistent") is False)
        past = [i for i in items if i.get("source") == "detached-job"
                and (_sched_at(i.get("next_at") or "") or gen) < gen - timedelta(hours=1)]
        stale_jobs += [(doc.get("node"), i) for i in past]
        enabled_ac = sum(1 for i in items if i.get("source") == "agent-cron" and i.get("enabled"))
        state = doc.get("error") or ("일부 실패" if doc.get("errors") else "ok")
        out.append(f"| {_cell(doc.get('node'))} | {timers} | {transient} | {c['cron']} | {c['detached-job']} | "
                   f"{len(past)} | {enabled_ac} | {_cell(state)} |")
        for i in items:
            for ref in i.get("issues") or []:
                key = (ref.get("repo") or "", int(ref.get("number") or 0))
                if key in open_keys or key in closed_keys:
                    linked.append((doc.get("node"), i, key))
    out.append("")

    out += [f"### 이슈에 걸린 예약 ({len(linked)}건)", "",
            "설명이나 이름에 `저장소#번호`로 적힌 이슈가 열려 있거나 최근 7일 안에 닫힌 것만 싣는다.", ""]
    if linked:
        out += ["| 노드 | 다음 실행 · 일정 | 종류 | 이름 | 설명 | 이슈 | 이슈 상태 |", "|---|---|---|---|---|---|---|"]
        for node, i, key in sorted(linked, key=lambda r: (r[0] or "", r[2])):
            ref = open_keys.get(key) or closed_keys.get(key)
            state = "닫힘" if key in closed_keys and key not in open_keys else BUCKET_TITLE.get(ref.get("_bucket", ""), "열림")
            kind = _SOURCE_TITLE.get(i.get("source"), i.get("source") or "")
            if i.get("persistent") is False and i.get("source", "").startswith("systemd"):
                kind += "(일시)"
            out.append(f"| {_cell(node)} | {_when(i)} | {_cell(kind)} | {_cell(i.get('name'), 60)} | "
                       f"{_cell(i.get('summary') or '—', 80)} | {_cell(key[0])} {_ref(ref)} | {_cell(state)} |")
    else:
        out.append("없음.")
    out.append("")

    on_closed = [(n, i, k) for n, i, k in linked if k in closed_keys and k not in open_keys]
    out += [f"### 닫힌 이슈에 걸린 예약 ({len(on_closed)}건)", "",
            "이슈는 닫혔는데 예약이 남아 있다. 끝난 확인이면 예약을 지운다(지우는 것은 그 노드의 일).", ""]
    if on_closed:
        out += ["| 노드 | 이름 | 종류 | 이슈 |", "|---|---|---|---|"]
        out += [f"| {_cell(n)} | {_cell(i.get('name'), 60)} | {_cell(_SOURCE_TITLE.get(i.get('source'), ''))} | "
                f"{_cell(k[0])} #{k[1]} |" for n, i, k in on_closed]
    else:
        out.append("없음.")
    out.append("")

    reached = [d for d in nodes if not d.get("error")]
    referenced = {k for _, _, k in linked}
    waiting = [i for i in issues if i.get("_bucket") == "date" and (i["repo"], i["number"]) not in referenced]
    out += [f"### 노드 예약이 안 보이는 날짜 대기 ({len(waiting)}건)", "",
            "날짜 대기 표식이 있지만 어느 노드의 예약 설명에도 이 이슈가 없다. 오너 입회 세션처럼 노드 예약이 "
            "필요 없는 일일 수 있다 — 결함이 아니라 확인용 목록이다. 예약을 걸 때 설명에 `저장소#번호`를 적으면 "
            f"여기서 빠진다. (수집된 노드 {len(reached)}/{len(nodes)})", ""]
    if waiting:
        out += [_STATUS_HEADER, "|---|---|---|---|---|---|"]
        out += [_status_row(i) for i in sorted(waiting, key=_by_due)]
    else:
        out.append("없음.")
    out.append("")

    out += [f"### 지난 예약 작업 — 확인 안 됨 ({len(stale_jobs)}건)", "",
            "예정 시각이 한 시간 넘게 지났는데 예약 작업 레지스트리에서 확인(ack)되지 않았다. 결과를 보고 "
            "`detached_jobs.py ack --unit <이름>`으로 닫는다.", ""]
    if stale_jobs:
        out += ["| 노드 | 이름 | 예정 | 설명 |", "|---|---|---|---|"]
        out += [f"| {_cell(n)} | {_cell(i.get('name'), 60)} | {_when(i)} | {_cell(i.get('summary') or '—', 80)} |"
                for n, i in sorted(stale_jobs, key=lambda r: (r[0] or "", r[1].get("next_at") or ""))]
    else:
        out.append("없음.")
    out.append("")


def _render_howto(out: list) -> None:
    out += [
        "## 표식 남기는 법", "",
        "보고할 때 이슈 코멘트 첫 줄에 한 줄을 둔다. 도우미: `python3 scripts/ccc_issue_status.py set --repo OWNER/REPO "
        "--issue N --waiting owner --node <노드> --next \"<한 줄>\"`.", "",
        "```",
        '<!-- status:v1 waiting=owner|node|date|external node=<노드> due=YYYY-MM-DD[THH:MM] next="<80자 이내 한 줄>" -->',
        "```", "",
        "`waiting=date`에는 `due`가 필요하다. `next`에는 다음 행동만 한국어로 적고 오너 지시 원문이나 코멘트 본문을 옮기지 않는다.",
        "",
        "예약된 확인이 결과를 이슈에 올릴 때는 같은 코멘트(또는 바로 다음 코멘트)에 새 표식을 남기거나 이슈를 닫는다. 결과만 올리고 표식을 두면 「표식 갱신 필요」에 오른다. 끝나지 않는 자동 보고 이슈에는 표식 대신 `board:standing` 라벨을 붙인다.",
        "",
    ]


def render(snapshot: dict, doc_id: str, today: Optional[date] = None) -> str:
    if snapshot.get("schema") != SCHEMA:
        raise BoardError("snapshot schema mismatch")
    if not re.match(r"^DOC-\d+$", doc_id or ""):
        raise BoardError("doc id must look like DOC-1234")
    gen = _kst(snapshot.get("generated_at") or "")
    if gen is None:
        raise BoardError("snapshot has no generated_at")
    today = today or gen.date()
    issues = [dict(i) for i in snapshot.get("items") or [] if i.get("kind") == "issue"]
    prs = [i for i in snapshot.get("items") or [] if i.get("kind") == "pr"]
    by_bucket: dict = defaultdict(list)
    for item in issues:
        item["_bucket"] = classify(item, today, gen)
        by_bucket[item["_bucket"]].append(item)
    stale_cut = (datetime.combine(today, datetime.min.time(), KST) - timedelta(days=STALE_DAYS))
    stale = [i for i in issues if (_kst(last_activity(i)) or gen) < stale_cut]
    out: list = []
    _render_head(out, doc_id, snapshot.get("org") or "", gen)
    _render_summary(out, issues, prs, by_bucket, stale, snapshot.get("counts") or {})
    _render_buckets(out, by_bucket)
    _render_schedules(out, snapshot.get("schedules"), issues, snapshot.get("closed"), gen)
    _render_closed(out, snapshot.get("closed"))
    _render_prs(out, prs)
    _render_nodes(out, issues)
    _render_repos(out, issues, prs, stale)
    _render_howto(out)
    return "\n".join(out)


# ---------------------------------------------------------------------------- publish

def _run(args: Sequence[str], cwd: Optional[Path] = None, timeout: int = 300) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(list(args), cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BoardError(f"{args[0]} failed: {type(exc).__name__}") from None


def _porcelain(worktree: Path) -> list:
    res = _run(["git", "-C", str(worktree), "-c", "core.quotePath=false", "status", "--porcelain=v1", "-z", "--untracked-files=all"])
    if res.returncode != 0:
        raise BoardError(f"git status failed rc={res.returncode}")
    return [e[3:] for e in res.stdout.split("\0") if e]


def next_doc_id(pages: Path) -> str:
    best = 0
    pat = re.compile(r"\[(?:DOC|TM|ND)-(\d+)\]")
    for path in pages.rglob("*.md"):
        try:
            for m in pat.finditer(path.read_text(encoding="utf-8", errors="replace")):
                best = max(best, int(m.group(1)))
        except OSError:
            continue
    return f"DOC-{best + 1}"


def _strip_generated(text: str) -> str:
    return "\n".join(ln for ln in text.splitlines() if not ln.startswith(GENERATED_LINE))


def publish(snapshot: dict, page: str = DEFAULT_PAGE, wiki_agent_bin: str = "wiki-agent",
            dry_run: bool = False) -> dict:
    if not re.match(r"^pages/[A-Za-z0-9._/-]+\.md$", page) or ".." in page:
        raise BoardError("--page must be a plain path under pages/")
    res = _run([wiki_agent_bin, "write-path"])
    if res.returncode != 0:
        raise BoardError(f"wiki-agent write-path failed rc={res.returncode}")
    lines = [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]
    if not lines:
        raise BoardError("wiki-agent write-path printed no path")
    worktree = Path(lines[-1])
    if not (worktree / "pages").is_dir():
        raise BoardError("wiki worktree has no pages/")
    if _porcelain(worktree):
        raise BoardError("wiki worktree is not clean before the board refresh")
    target = worktree / page
    if target.is_symlink():
        raise BoardError("target page is a symlink")
    old = target.read_text(encoding="utf-8") if target.exists() else ""
    m = re.match(r"^# \[(DOC-\d+)\]", old)
    doc_id = m.group(1) if m else next_doc_id(worktree / "pages")
    new = render(snapshot, doc_id)
    if old and _strip_generated(old) == _strip_generated(new):
        return {"ok": True, "changed": False, "page": page, "doc_id": doc_id}
    if dry_run:
        return {"ok": True, "changed": True, "dry_run": True, "page": page, "doc_id": doc_id, "bytes": len(new.encode())}
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".work-board.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(new)
        os.chmod(tmp, 0o644)
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    changed = _porcelain(worktree)
    unexpected = [p for p in changed if p != page]
    if unexpected:
        raise BoardError(f"unexpected paths changed: {unexpected[:3]}")
    res = _run([wiki_agent_bin, "pr"], cwd=worktree, timeout=600)
    if res.returncode != 0:
        raise BoardError(f"wiki-agent pr failed rc={res.returncode}")
    return {"ok": True, "changed": True, "page": page, "doc_id": doc_id, "submitted": True}


# ---------------------------------------------------------------------------- CLI

def _fleet(value: str) -> list:
    names = [v for v in re.split(r"[,\s]+", value or "") if v]
    for n in names:
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$", n):
            raise argparse.ArgumentTypeError("bad login in --fleet-authors")
    return names or list(DEFAULT_FLEET_AUTHORS)


def _summary(snapshot: dict, result: dict) -> str:
    issues = [i for i in snapshot["items"] if i.get("kind") == "issue"]
    gen = _kst(snapshot["generated_at"])
    today = gen.date() if gen else date.today()
    c = Counter(classify(i, today) for i in issues)
    state = "갱신" if result.get("changed") else "변경 없음"
    return (f"현황판 {state}: 열린 이슈 {len(issues)} · 오너 차례 {c.get('owner', 0)} · 날짜 대기 {c.get('date', 0)} · "
            f"기한 지남 {c.get('overdue', 0)} · 분류 안 됨 {c.get('unclassified', 0)}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="fleet work board (#2914)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("collect")
    p.add_argument("--org", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--gh-bin", default="gh")
    p.add_argument("--fleet-authors", type=_fleet, default=_fleet(os.environ.get("CCC_WORK_BOARD_FLEET_AUTHORS", "")))
    p.add_argument("--schedules", default="", help="attach a schedules document (from the schedules command)")
    p = sub.add_parser("schedules")
    p.add_argument("--nodes", required=True, help="space/comma separated node names (ssh aliases)")
    p.add_argument("--self", dest="self_node", default="", help="this hub's own node name (collected without ssh)")
    p.add_argument("--out", required=True)
    p.add_argument("--ssh-bin", default="ssh")
    p = sub.add_parser("render")
    p.add_argument("--snapshot", required=True)
    p.add_argument("--doc-id", default="DOC-0")
    p.add_argument("--out", default="-")
    for name in ("publish", "run"):
        p = sub.add_parser(name)
        if name == "publish":
            p.add_argument("--snapshot", required=True)
        else:
            p.add_argument("--org", required=True)
            p.add_argument("--gh-bin", default="gh")
            p.add_argument("--fleet-authors", type=_fleet,
                           default=_fleet(os.environ.get("CCC_WORK_BOARD_FLEET_AUTHORS", "")))
            p.add_argument("--snapshot-out", default="")
            p.add_argument("--schedule-nodes", default=os.environ.get("CCC_WORK_BOARD_SCHEDULE_NODES", ""),
                           help="also collect node schedules over ssh (stage 3); empty = skip")
            p.add_argument("--self-node", default=os.environ.get("CCC_WORK_BOARD_SELF_NODE", ""))
            p.add_argument("--state-dir", default=os.environ.get("CCC_STATE_DIR", str(Path.home() / ".claude/state")))
        p.add_argument("--page", default=DEFAULT_PAGE)
        p.add_argument("--wiki-agent-bin", default="wiki-agent")
        p.add_argument("--dry-run", action="store_true")
        p.add_argument("--result-file", default="")
    args = ap.parse_args(argv)

    try:
        if args.cmd == "collect":
            snap = collect(args.org, args.gh_bin, args.fleet_authors)
            if args.schedules:
                snap["schedules"] = json.loads(Path(args.schedules).read_text(encoding="utf-8"))
            Path(args.out).write_text(json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")
            print(json.dumps({"ok": True, "items": len(snap["items"]), "repos": len(snap["repos"])}))
            return 0
        if args.cmd == "schedules":
            doc = collect_schedules(_node_list(args.nodes), args.self_node, args.ssh_bin)
            Path(args.out).write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
            failed = [d["node"] for d in doc["nodes"] if d.get("error")]
            print(json.dumps({"ok": True, "nodes": len(doc["nodes"]), "failed": failed}, ensure_ascii=False))
            return 0
        if args.cmd == "render":
            snap = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
            page = render(snap, args.doc_id if args.doc_id != "DOC-0" else "DOC-1")
            if args.out == "-":
                sys.stdout.write(page)
            else:
                Path(args.out).write_text(page, encoding="utf-8")
            return 0
        lock = None
        if args.cmd == "run":
            state = Path(args.state_dir)
            state.mkdir(parents=True, exist_ok=True)
            lock = open(state / "work-board.lock", "w")
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise BoardError("another work-board run holds the lock") from None
            snap = collect(args.org, args.gh_bin, args.fleet_authors)
            if args.schedule_nodes:
                snap["schedules"] = collect_schedules(_node_list(args.schedule_nodes), args.self_node)
            if args.snapshot_out:
                Path(args.snapshot_out).write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
        else:
            snap = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
        result = publish(snap, args.page, args.wiki_agent_bin, args.dry_run)
        line = _summary(snap, result)
        if args.result_file:
            Path(args.result_file).write_text(line + "\n", encoding="utf-8")
        print(json.dumps({**result, "summary": line}, ensure_ascii=False))
        return 0
    except BoardError as exc:
        if getattr(args, "result_file", ""):
            Path(args.result_file).write_text(f"현황판 실패: {exc}\n", encoding="utf-8")
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
