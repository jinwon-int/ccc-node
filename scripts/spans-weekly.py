#!/usr/bin/env python3
"""Weekly fleet span summary -> one Family Wiki page (#2899).

The hub already keeps one checked, body-free record per node and KST day
(``spans-view-collect.sh`` -> ``spans-view.py build`` ->
``<state>/spans-view/data/YYYY-MM-DD/<node>.json`` plus ``_status.json``).
Once a week (Monday, after the 04:30 daily collection) this script folds the
previous Monday-Sunday of those records into one Markdown section and adds it
to a Wiki page through the normal PR-first path (``wiki-agent write-path`` ->
edit -> ``wiki-agent pr``). It opens no connection to any node.

  spans-weekly.py [--week YYYY-Www] [--nodes "a b c"] [--dry-run] [--page P] ...

What a section holds (numbers and identifier-shaped names only):

- per node: days collected (n/7), turns, tool calls, tool error rate, cost
  (USD), the largest daily cost-ledger difference, a2a ok/failed when any
  node reported a2a spans; one fleet total row;
- the five tools with the most errors (fleet-wide): calls, errors, rate, the
  largest daily p95;
- "big changes" against the week before: the tool error rate rose by
  ``--error-rate-pp`` points or more, or turns per collected day moved to
  ``--turns-low`` x or below / ``--turns-high`` x or above (provisional
  thresholds, also settable through ``CCC_SPANS_WEEKLY_*``);
- days not collected per node, with the reason codes the collector recorded.

A week with no collected day at all gets a single 「수집 없음」 line.

Page rules:

- every section starts with ``<!-- span-weekly:YYYY-Www -->`` on its own
  line; a week that already has that marker on the page or in its yearly
  archive is never written again;
- the page keeps the newest 12 weeks, newest first; older sections move to
  ``<page stem>-archive-<ISO year>.md``;
- the top of the page holds one ``에이전트 다이제스트`` line for the newest
  week so ``wiki-agent prefetch`` / ``find`` pick it up;
- the page's ``[DOC-N]`` id is allocated from the worktree when the page is
  first created (the same rule as the work board, #2914).

``--dry-run`` prints the target week's Markdown and nothing else: no Wiki
worktree is opened and no PR is made. ``--catch-up N`` (default 1) also adds
up to N earlier weeks still missing from the page, so a week whose PR failed
is retried on the next run. ``CCC_SPANS_VIEW_NOW`` (ISO timestamp) replaces
the clock; it is a test seam shared with spans-view.py.

Exit 0 on success (including "nothing to add"), 2 on a usage or input error,
1 when the Wiki step failed.
"""
from __future__ import annotations

import argparse
import fcntl
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional, Sequence

HERE = Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location("ccc_spans_view", HERE / "spans-view.py")
assert _SPEC is not None and _SPEC.loader is not None
view = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(view)

KST = view.KST
DEFAULT_PAGE = "pages/services/span-rollup.md"
KEEP_WEEKS = 12
LOOKBACK_WEEKS = 11  # the hub keeps 90 days of records: 12 weeks back from the latest
MAX_CATCH_UP = 11
MAX_NODES = 40
TOP_TOOLS = 5
MAX_NAME = 60
MAX_SECTION_BYTES = 24 * 1024
DEFAULT_ERROR_PP = 2.0
DEFAULT_TURNS_LOW = 0.5
DEFAULT_TURNS_HIGH = 2.0

_WEEK = re.compile(r"^\d{4}-W\d{2}$")
_MARKER = re.compile(r"^<!-- span-weekly:(\d{4}-W\d{2}) -->$", re.M)
_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9._:/@+-]")
_PAGE = re.compile(r"^pages/[A-Za-z0-9._/-]+\.md$")
DIGEST_LINE = "> **에이전트 다이제스트** — status: `active` · "


class WeeklyError(Exception):
    """The Wiki step failed; exit 1."""


class UsageError(ValueError):
    """Bad arguments or input; exit 2."""


def marker(week: str) -> str:
    return f"<!-- span-weekly:{week} -->"


# ---------------------------------------------------------------------- weeks


def week_id(monday: date) -> str:
    return monday.strftime("%G-W%V")


def parse_week(text: str) -> date:
    if not _WEEK.match(text or ""):
        raise UsageError("bad --week (want YYYY-Www)")
    try:
        monday = datetime.strptime(text + "-1", "%G-W%V-%u").date()
    except ValueError as exc:
        raise UsageError("bad --week (want YYYY-Www)") from exc
    if week_id(monday) != text:  # W53 of a 52-week year rolls over
        raise UsageError("bad --week (no such ISO week)")
    return monday


def latest_week(now: datetime) -> date:
    """Monday of the last complete Monday-Sunday week (KST)."""
    today = now.astimezone(KST).date()
    return today - timedelta(days=today.weekday() + 7)


def check_week(monday: date, now: datetime) -> date:
    latest = latest_week(now)
    if monday > latest:
        raise UsageError("--week is not complete yet (KST)")
    if monday < latest - timedelta(weeks=LOOKBACK_WEEKS):
        raise UsageError("--week is older than the kept records")
    return monday


def week_range(monday: date) -> str:
    return f"{monday.isoformat()} ~ {(monday + timedelta(days=6)).isoformat()}"


# ----------------------------------------------------------------------- data


def _name(value: Any) -> str:
    """An identifier-shaped tool name: no Markdown, no HTML, no pipe, bounded."""
    text = _NAME_UNSAFE.sub("?", str(value))[:MAX_NAME]
    return text or "?"


def _fields(record: dict) -> dict:
    """The numbers this summary needs from one checked record, re-checked.

    Raises ``view.Invalid`` (or TypeError/AttributeError) on a wrong shape.
    """
    totals = record["totals"]
    out = {
        "turns": view._int(totals.get("turns")),
        "calls": view._int(totals.get("tool_calls")),
        "errors": view._int(totals.get("tool_errors")),
        "cost": sum(float(view._num(model.get("cost_usd"))) for model in record.get("models") or []),
        "tools": [],
        "ledger": None,
        "a2a": None,
    }
    for tool in record.get("tools") or []:
        out["tools"].append({"name": _name(tool.get("name")), "calls": view._int(tool.get("calls")),
                             "errors": view._int(tool.get("errors")),
                             "p95": view._num(tool.get("p95_ms"), optional=True)})
    ledger = record.get("ledger") or {}
    if ledger.get("available") is True:
        if not isinstance(ledger.get("within_target"), bool):
            raise view.Invalid("schema")
        out["ledger"] = {"max": view._num(ledger.get("max_diff_pct"), optional=True),
                         "within": ledger["within_target"]}
    a2a = record.get("a2a")
    if a2a is not None:
        out["a2a"] = {"ok": view._int(a2a.get("ok")), "failed": view._int(a2a.get("failed"))}
    return out


def _empty_node() -> dict:
    return {"days": 0, "turns": 0, "calls": 0, "errors": 0, "cost": 0.0, "tools": {},
            "ledger_days": 0, "ledger_max": None, "ledger_over": 0,
            "a2a": None, "missing": Counter()}


def _add(node: dict, fields: dict) -> None:
    node["days"] += 1
    for key in ("turns", "calls", "errors", "cost"):
        node[key] += fields[key]
    for tool in fields["tools"]:
        slot = node["tools"].setdefault(tool["name"], {"calls": 0, "errors": 0, "p95": None})
        slot["calls"] += tool["calls"]
        slot["errors"] += tool["errors"]
        if tool["p95"] is not None:
            slot["p95"] = tool["p95"] if slot["p95"] is None else max(slot["p95"], tool["p95"])
    ledger = fields["ledger"]
    if ledger is not None:
        node["ledger_days"] += 1
        node["ledger_over"] += 0 if ledger["within"] else 1
        if ledger["max"] is not None:
            node["ledger_max"] = ledger["max"] if node["ledger_max"] is None else max(node["ledger_max"], ledger["max"])
    if fields["a2a"] is not None:
        node["a2a"] = node["a2a"] or {"ok": 0, "failed": 0}
        node["a2a"]["ok"] += fields["a2a"]["ok"]
        node["a2a"]["failed"] += fields["a2a"]["failed"]


def week_nodes(data_dir: Path, monday: date) -> list[str]:
    """Every node the hub's status files name for the week (when --nodes is not given)."""
    seen: set[str] = set()
    for offset in range(7):
        seen.update(view.load_status(data_dir, (monday + timedelta(days=offset)).isoformat()))
    return sorted(seen)[:MAX_NODES]


def load_week(data_dir: Path, monday: date, nodes: list[str]) -> dict[str, dict]:
    """Per-node week totals from the hub's records; a day not collected is a reason code."""
    result = {node: _empty_node() for node in nodes}
    for offset in range(7):
        day = (monday + timedelta(days=offset)).isoformat()
        ran = os.path.lexists(data_dir / day / "_status.json")
        status = view.load_status(data_dir, day) if ran else {}
        for node in nodes:
            slot = status.get(node)
            if not ran:
                result[node]["missing"]["no-run"] += 1
                continue
            if slot is None:
                result[node]["missing"]["no-result"] += 1
                continue
            if slot["state"] not in view.COLLECTED:
                result[node]["missing"][slot["detail"] or _name(slot["state"])] += 1
                continue
            record = view.load_record(data_dir, day, node)
            try:
                fields = _fields(record) if record is not None else None
            except (view.Invalid, TypeError, AttributeError, KeyError, ValueError):
                fields = None
            if fields is None:
                result[node]["missing"]["record-invalid"] += 1
                continue
            _add(result[node], fields)
    return result


# ------------------------------------------------------------------- render


def _n(value: int) -> str:
    return f"{value:,}"


def _rate(errors: int, calls: int) -> Optional[float]:
    return 100.0 * errors / calls if calls else None


def _pct(value: Optional[float]) -> str:
    return "-" if value is None else f"{value:.1f}%"


def _usd(value: float) -> str:
    return f"${value:,.2f}"


def _ledger_cell(node: dict) -> str:
    if not node["ledger_days"]:
        return "원장 없음"
    text = "비교 불가" if node["ledger_max"] is None else f"{node['ledger_max']:.1f}%"
    return text + (f" (초과 {node['ledger_over']}일)" if node["ledger_over"] else "")


def _a2a_cell(a2a: Optional[dict]) -> str:
    return "-" if a2a is None else f"{a2a['ok']:,}/{a2a['failed']:,}"


def big_changes(nodes: list[str], cur: dict, prev: dict, pp: float, low: float, high: float) -> list[str]:
    lines = []
    for node in nodes:
        now, before = cur[node], prev.get(node)
        if not now["days"] or not before or not before["days"]:
            continue
        notes = []
        rate_now, rate_before = _rate(now["errors"], now["calls"]), _rate(before["errors"], before["calls"])
        if rate_now is not None and rate_before is not None and rate_now - rate_before >= pp - 1e-9:
            notes.append(f"도구 오류율 {_pct(rate_before)} → {_pct(rate_now)} (+{rate_now - rate_before:.1f}%p)")
        avg_now, avg_before = now["turns"] / now["days"], before["turns"] / before["days"]
        if avg_before > 0:
            ratio = avg_now / avg_before
            if ratio <= low + 1e-9 or ratio >= high - 1e-9:
                notes.append(f"수집일 평균 턴 {avg_before:,.1f} → {avg_now:,.1f} ({ratio:.2f}배)")
        if notes:
            lines.append(f"- {node} — " + " · ".join(notes))
    return lines


def _reasons(missing: Counter) -> str:
    return " · ".join(f"{code} {count}" for code, count in sorted(missing.items(), key=lambda kv: (-kv[1], kv[0])))


def _node_table(nodes: list[str], cur: dict, totals: dict) -> list[str]:
    with_a2a = any(cur[n]["a2a"] is not None for n in nodes)
    columns = ["노드", "수집일", "턴", "도구 호출", "도구 오류율", "비용(USD)", "원장 차이 최대"]
    if with_a2a:
        columns.append("a2a 성공/실패")
    out = ["### 노드별", "", "| " + " | ".join(columns) + " |", "|---|---|" + "---:|" * (len(columns) - 2)]
    for node in nodes:
        slot = cur[node]
        if slot["days"]:
            row = [node, f"{slot['days']}/7", _n(slot["turns"]), _n(slot["calls"]),
                   _pct(_rate(slot["errors"], slot["calls"])), _usd(slot["cost"]), _ledger_cell(slot)]
        else:
            row = [node, "0/7", "-", "-", "-", "-", "-"]
        if with_a2a:
            row.append(_a2a_cell(slot["a2a"]) if slot["days"] else "-")
        out.append("| " + " | ".join(row) + " |")
    total = ["**플릿 합계**", f"{totals['days']}/{7 * len(nodes)}", _n(totals["turns"]), _n(totals["calls"]),
             _pct(_rate(totals["errors"], totals["calls"])), _usd(totals["cost"]), "-"]
    if with_a2a:
        seen = [cur[n]["a2a"] for n in nodes if cur[n]["days"] and cur[n]["a2a"] is not None]
        total.append(_a2a_cell({"ok": sum(a["ok"] for a in seen), "failed": sum(a["failed"] for a in seen)}))
    return out + ["| " + " | ".join(total) + " |", ""]


def _tool_table(nodes: list[str], cur: dict) -> list[str]:
    tools: dict[str, dict] = {}
    for node in nodes:
        for name, slot in cur[node]["tools"].items():
            agg = tools.setdefault(name, {"calls": 0, "errors": 0, "p95": None})
            agg["calls"] += slot["calls"]
            agg["errors"] += slot["errors"]
            if slot["p95"] is not None:
                agg["p95"] = slot["p95"] if agg["p95"] is None else max(agg["p95"], slot["p95"])
    failing = sorted(((name, slot) for name, slot in tools.items() if slot["errors"]),
                     key=lambda kv: (-kv[1]["errors"], -(_rate(kv[1]["errors"], kv[1]["calls"]) or 0), kv[0]))
    out = [f"### 오류가 많은 도구 상위 {TOP_TOOLS} (플릿 합산)", ""]
    if not failing:
        return out + ["도구 오류 없음.", ""]
    out += ["| 도구 | 호출 | 오류 | 오류율 | p95 ms (일별 최대) |", "|---|---:|---:|---:|---:|"]
    for name, slot in failing[:TOP_TOOLS]:
        p95 = "-" if slot["p95"] is None else f"{slot['p95']:,.0f}"
        out.append(f"| `{name}` | {_n(slot['calls'])} | {_n(slot['errors'])} | "
                   f"{_pct(_rate(slot['errors'], slot['calls']))} | {p95} |")
    return out + [""]


def render_section(monday: date, nodes: list[str], cur: dict, prev: dict,
                   pp: float = DEFAULT_ERROR_PP, low: float = DEFAULT_TURNS_LOW,
                   high: float = DEFAULT_TURNS_HIGH) -> str:
    week = week_id(monday)
    out = [marker(week), f"## {week} · {week_range(monday)} (KST)", ""]
    collected = [node for node in nodes if cur[node]["days"]]
    totals = {key: sum(cur[n][key] for n in collected) for key in ("days", "turns", "calls", "errors", "cost")}
    if not totals["days"]:
        reasons: Counter = Counter()
        for node in nodes:
            reasons.update(cur[node]["missing"])
        why = _reasons(reasons)
        out.append(f"- 수집 없음 — 이 주에는 수집된 날이 없습니다 (노드 {len(nodes)}"
                   + (f" · {why}" if why else "") + ").")
        return "\n".join(out) + "\n"

    prev_week = week_id(monday - timedelta(weeks=1))
    prev_days = sum(slot["days"] for slot in prev.values())
    changes = big_changes(nodes, cur, prev, pp, low, high) if prev_days else []
    out += [f"- 요약: 수집 노드 {len(collected)}/{len(nodes)} · 수집일 {totals['days']}/{7 * len(nodes)} · "
            f"턴 {_n(totals['turns'])} · 도구 호출 {_n(totals['calls'])} · "
            f"도구 오류율 {_pct(_rate(totals['errors'], totals['calls']))} · "
            f"비용 {_usd(totals['cost'])} · 큰 변화 {len(changes)}건", ""]
    out += _node_table(nodes, cur, totals)
    out += _tool_table(collected, cur)
    out += ["### 전주 대비 큰 변화", "",
            f"기준(잠정): 도구 오류율 +{pp:g}%p 이상, 또는 수집일 평균 턴 {low:g}배 이하·{high:g}배 이상. "
            f"비교 대상 {prev_week}.", ""]
    if not prev_days:
        out.append(f"전주({prev_week}) 수집 기록이 없어 비교하지 않았습니다.")
    else:
        out += changes or ["없음."]
    out += ["", "### 미수집", ""]
    gaps = [f"- {node} — {sum(cur[node]['missing'].values())}일: {_reasons(cur[node]['missing'])}"
            for node in nodes if cur[node]["missing"]]
    out += gaps or ["없음 — 모든 노드를 7일 모두 수집했습니다."]
    text = "\n".join(out) + "\n"
    if len(text.encode("utf-8")) > MAX_SECTION_BYTES:
        raise UsageError("week section is larger than the size cap")
    return text


def build_section(data_dir: Path, monday: date, nodes: Optional[list[str]], pp: float,
                  low: float, high: float) -> str:
    names = nodes if nodes else week_nodes(data_dir, monday)
    cur = load_week(data_dir, monday, names)
    prev = load_week(data_dir, monday - timedelta(weeks=1), names)
    return render_section(monday, names, cur, prev, pp, low, high)


# --------------------------------------------------------------------- pages


def split_page(text: str) -> tuple[str, list[tuple[str, str]]]:
    """``(head, [(week, section text), ...])``; a repeated week keeps its first copy."""
    found = list(_MARKER.finditer(text))
    head = text[: found[0].start()] if found else text
    sections: list[tuple[str, str]] = []
    seen: set[str] = set()
    for index, match in enumerate(found):
        end = found[index + 1].start() if index + 1 < len(found) else len(text)
        if match.group(1) in seen:
            continue
        seen.add(match.group(1))
        sections.append((match.group(1), text[match.start(): end].rstrip("\n") + "\n"))
    return head, sections


def weeks_on(text: str) -> set[str]:
    return set(_MARKER.findall(text))


def archive_path(page: str, year: str) -> str:
    return f"{page[:-3]}-archive-{year}.md"


def _doc_id(head: str) -> Optional[str]:
    match = re.match(r"^# \[(DOC-\d+)\]", head)
    return match.group(1) if match else None


def next_doc_number(pages: Path) -> int:
    """One past the largest DOC/TM/ND number in the worktree (the work-board rule)."""
    best = 0
    pattern = re.compile(r"\[(?:DOC|TM|ND)-(\d+)\]")
    for path in pages.rglob("*.md"):
        try:
            for match in pattern.finditer(path.read_text(encoding="utf-8", errors="replace")):
                best = max(best, int(match.group(1)))
        except OSError:
            continue
    return best + 1


def digest(section: str) -> str:
    lines = section.splitlines()
    heading = lines[1][3:] if len(lines) > 1 and lines[1].startswith("## ") else "?"
    body = next((line[2:] for line in lines[2:] if line.startswith("- ")), "")
    body = body[len("요약: "):] if body.startswith("요약: ") else body
    return f"{DIGEST_LINE}최신 주 {heading} — {body}"


def page_head(doc_id: str, page: str, newest: str) -> str:
    return "\n".join([
        f"# [{doc_id}] 스팬 주간 플릿 요약 (자동 생성)",
        "",
        digest(newest),
        "> - 이 페이지: 허브가 매일 모으는 노드별 스팬 일일 집계를 주 1회(월요일) 묶은 플릿 요약이다. "
        f"최근 {KEEP_WEEKS}주를 최신순으로 두고, 더 오래된 주는 연도별 보관 페이지 "
        f"`{archive_path(page, 'YYYY')}`로 옮긴다. **손으로 고치지 말 것** — 주마다 생성기가 절을 더한다.",
        "> - 근거: 허브의 spans-view 수집 기록(`spans-view/data/<날짜>/<노드>.json`, `_status.json`). "
        "숫자와 도구 이름만 싣고 대화 본문·프롬프트·도구 입력은 싣지 않는다. "
        "생성기: ccc-node `scripts/spans-weekly.py`(ccc-node#2899).",
        "> - 읽는 법: 수집일은 n/7, 도구 오류율은 그 주 도구 호출 대비 오류, 원장 차이는 하루 단위 "
        "비용 원장 교차검증의 최대값이다. 「큰 변화」 기준은 잠정값이다.",
        "",
        "",
    ])


def archive_head(doc_id: str, year: str, page: str) -> str:
    return "\n".join([
        f"# [{doc_id}] 스팬 주간 플릿 요약 — {year} 보관 (자동 생성)",
        "",
        f"> - `{page}`에서 {KEEP_WEEKS}주가 지나 옮겨 온 {year}년(ISO 주 기준) 주간 절. 최신순. "
        "**손으로 고치지 말 것.**",
        "",
        "",
    ])


def _join(head: str, sections: list[tuple[str, str]]) -> str:
    return head + "\n".join(text for _, text in sections)


def merge_pages(page: str, read, add: dict[str, str], next_number: int) -> dict[str, str]:
    """New text for every page that changes. ``read(path)`` returns the current text or ''."""
    old = read(page)
    head, sections = split_page(old)
    archives: dict[str, str] = {}

    def archived(year: str) -> str:
        path = archive_path(page, year)
        if path not in archives:
            archives[path] = read(path)
        return archives[path]

    if old and _doc_id(old) is None:
        raise WeeklyError("the page exists but was not made by this generator; nothing changed")
    fresh = {week: text for week, text in add.items()
             if week not in {w for w, _ in sections} and week not in weeks_on(archived(week[:4]))}
    if not fresh:
        return {}
    sections = sorted(sections + list(fresh.items()), key=lambda kv: kv[0], reverse=True)
    keep, moved = sections[:KEEP_WEEKS], sections[KEEP_WEEKS:]
    changes: dict[str, str] = {}
    number = next_number

    doc_id = _doc_id(old)
    if doc_id is None:
        doc_id, number = f"DOC-{number}", number + 1
    changes[page] = page_head(doc_id, page, keep[0][1]) + "\n".join(text for _, text in keep)

    by_year: dict[str, list[tuple[str, str]]] = {}
    for week, text in moved:
        by_year.setdefault(week[:4], []).append((week, text))
    for year in sorted(by_year):
        path = archive_path(page, year)
        old_archive = archived(year)
        if old_archive and _doc_id(old_archive) is None:
            raise WeeklyError("an archive page exists but was not made by this generator; nothing changed")
        a_head, a_sections = split_page(old_archive)
        present = {w for w, _ in a_sections}
        merged = sorted(a_sections + [kv for kv in by_year[year] if kv[0] not in present],
                        key=lambda kv: kv[0], reverse=True)
        a_id = _doc_id(old_archive)
        if a_id is None:
            a_id, number = f"DOC-{number}", number + 1
            a_head = archive_head(a_id, year, page)
        changes[path] = _join(a_head, merged)
    return changes


# ------------------------------------------------------------------- publish


def _run(args: Sequence[str], cwd: Optional[Path] = None, timeout: int = 300) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(list(args), cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WeeklyError(f"{Path(args[0]).name} failed: {type(exc).__name__}") from None


def _porcelain(worktree: Path) -> list[str]:
    res = _run(["git", "-C", str(worktree), "-c", "core.quotePath=false", "status",
                "--porcelain=v1", "-z", "--untracked-files=all"])
    if res.returncode != 0:
        raise WeeklyError(f"git status failed rc={res.returncode}")
    return [entry[3:] for entry in res.stdout.split("\0") if entry]


def _inside(worktree: Path, rel: str) -> Path:
    target = worktree / rel
    root = os.path.realpath(worktree)
    if target.is_symlink() or not os.path.realpath(target).startswith(root + os.sep):
        raise WeeklyError("target page is a symlink or leaves the worktree")
    return target


def _write(target: Path, text: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".span-weekly.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def publish(weeks: list[date], render, page: str = DEFAULT_PAGE,
            wiki_agent_bin: str = "wiki-agent") -> dict:
    """Add the weeks missing from the page (``render(monday)`` -> section) through a Wiki PR."""
    if not _PAGE.match(page) or ".." in page:
        raise UsageError("--page must be a plain path under pages/")
    res = _run([wiki_agent_bin, "write-path"])
    if res.returncode != 0:
        raise WeeklyError(f"wiki-agent write-path failed rc={res.returncode}")
    lines = [line.strip() for line in res.stdout.splitlines() if line.strip()]
    if not lines:
        raise WeeklyError("wiki-agent write-path printed no path")
    worktree = Path(lines[-1])
    if not (worktree / "pages").is_dir():
        raise WeeklyError("wiki worktree has no pages/")
    if _porcelain(worktree):
        raise WeeklyError("wiki worktree is not clean before the weekly summary")

    def read(rel: str) -> str:
        target = _inside(worktree, rel)
        return target.read_text(encoding="utf-8") if target.exists() else ""

    main_text = read(page)
    wanted = [w for w in weeks
              if week_id(w) not in weeks_on(main_text)
              and week_id(w) not in weeks_on(read(archive_path(page, week_id(w)[:4])))]
    names = [week_id(w) for w in weeks]
    if not wanted:
        return {"ok": True, "changed": False, "page": page, "weeks": [], "present": names}
    add = {week_id(w): render(w) for w in wanted}
    changes = merge_pages(page, read, add, next_doc_number(worktree / "pages"))
    if not changes:
        return {"ok": True, "changed": False, "page": page, "weeks": [], "present": names}
    for rel, text in changes.items():
        _write(_inside(worktree, rel), text)
    unexpected = [path for path in _porcelain(worktree) if path not in changes]
    if unexpected:
        raise WeeklyError(f"unexpected paths changed: {unexpected[:3]}")
    res = _run([wiki_agent_bin, "pr"], cwd=worktree, timeout=900)
    if res.returncode != 0:
        raise WeeklyError(f"wiki-agent pr failed rc={res.returncode}")
    archived = sorted(path for path in changes if path != page)
    return {"ok": True, "changed": True, "submitted": True, "page": page,
            "weeks": sorted(add, reverse=True), "archives": archived}


# ----------------------------------------------------------------------- CLI


def _env_float(name: str, default: float) -> float:
    text = os.environ.get(name, "")
    try:
        return float(text) if text else default
    except ValueError:
        raise UsageError(f"bad {name}") from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Weekly fleet span summary -> Family Wiki page (#2899).")
    parser.add_argument("--week", help="ISO week YYYY-Www (default: the last complete week, KST)")
    parser.add_argument("--nodes", help="space-separated node names (default: $CCC_FLEET_NODES, "
                                        "else every node the week's status files name)")
    parser.add_argument("--state-dir", default=os.environ.get("CCC_STATE_DIR") or str(Path.home() / ".claude" / "state"))
    parser.add_argument("--data-dir", help="record folder (default: <state-dir>/spans-view/data)")
    parser.add_argument("--page", default=os.environ.get("CCC_SPANS_WEEKLY_PAGE") or DEFAULT_PAGE)
    parser.add_argument("--catch-up", type=int, default=1,
                        help=f"also add up to N earlier weeks missing from the page (0-{MAX_CATCH_UP})")
    parser.add_argument("--error-rate-pp", type=float, default=None)
    parser.add_argument("--turns-low", type=float, default=None)
    parser.add_argument("--turns-high", type=float, default=None)
    parser.add_argument("--wiki-agent-bin", default="wiki-agent")
    parser.add_argument("--result-file", default="")
    parser.add_argument("--dry-run", action="store_true", help="print the week's Markdown only")
    return parser


def _thresholds(args: argparse.Namespace) -> tuple[float, float, float]:
    pp = args.error_rate_pp if args.error_rate_pp is not None else _env_float("CCC_SPANS_WEEKLY_ERROR_PP", DEFAULT_ERROR_PP)
    low = args.turns_low if args.turns_low is not None else _env_float("CCC_SPANS_WEEKLY_TURNS_LOW", DEFAULT_TURNS_LOW)
    high = args.turns_high if args.turns_high is not None else _env_float("CCC_SPANS_WEEKLY_TURNS_HIGH", DEFAULT_TURNS_HIGH)
    if not 0 < pp <= 100 or not 0 < low < 1 or not 1 < high <= 100:
        raise UsageError("thresholds out of range (0<pp<=100, 0<turns-low<1, 1<turns-high<=100)")
    return pp, low, high


def _data_dir(args: argparse.Namespace) -> Path:
    path = Path(args.data_dir) if args.data_dir else Path(args.state_dir) / "spans-view" / "data"
    if path.is_symlink() or not path.is_dir():
        raise UsageError("no spans-view records folder (is the daily view installed on this node?)")
    return path


def _nodes(args: argparse.Namespace) -> Optional[list[str]]:
    text = args.nodes if args.nodes is not None else os.environ.get("CCC_FLEET_NODES", "")
    if not text.strip():
        return None
    try:
        nodes = view.parse_nodes(text)
    except view.UsageError as exc:
        raise UsageError(str(exc)) from None
    if len(nodes) > MAX_NODES:
        raise UsageError(f"at most {MAX_NODES} nodes")
    return nodes


def _summary(result: dict, target: str) -> str:
    if result.get("changed"):
        text = "스팬 주간 요약 PR 제출: " + ", ".join(result["weeks"])
        return text + (f" (보관 이동: {', '.join(result['archives'])})" if result.get("archives") else "")
    return f"스팬 주간 요약 변경 없음: {target} 이미 있음"


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    lock = None
    try:
        now = view.clock()
        pp, low, high = _thresholds(args)
        monday = check_week(parse_week(args.week), now) if args.week else latest_week(now)
        if not 0 <= args.catch_up <= MAX_CATCH_UP:
            raise UsageError(f"--catch-up out of range (0-{MAX_CATCH_UP})")
        data_dir = _data_dir(args)
        nodes = _nodes(args)

        def render(week: date) -> str:
            return build_section(data_dir, week, nodes, pp, low, high)

        if args.dry_run:
            sys.stdout.write(render(monday))
            return 0
        oldest = latest_week(now) - timedelta(weeks=LOOKBACK_WEEKS)
        weeks = [monday - timedelta(weeks=k) for k in range(args.catch_up, -1, -1)]
        weeks = [w for w in weeks if w >= oldest]
        state = Path(args.state_dir)
        state.mkdir(parents=True, exist_ok=True)
        lock = open(state / "spans-weekly.lock", "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise WeeklyError("another spans-weekly run holds the lock") from None
        result = publish(weeks, render, args.page, args.wiki_agent_bin)
        line = _summary(result, week_id(monday))
        if args.result_file:
            Path(args.result_file).write_text(line + "\n", encoding="utf-8")
        print(json.dumps({**result, "summary": line}, ensure_ascii=False))
        return 0
    except (UsageError, view.UsageError) as exc:
        code, error = 2, str(exc)
    except WeeklyError as exc:
        code, error = 1, str(exc)
    finally:
        if lock is not None:
            lock.close()
    if args.result_file:
        Path(args.result_file).write_text(f"스팬 주간 요약 실패: {error}\n", encoding="utf-8")
    print(json.dumps({"ok": False, "error": error}, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
