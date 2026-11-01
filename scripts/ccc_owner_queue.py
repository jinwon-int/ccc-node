#!/usr/bin/env python3
"""Owner action queue — one list of everything that waits on the owner (#2617).

Work across many repositories stalls on the one person who approves merges,
checks real devices, holds secrets and cuts releases. Those waits are scattered
over PRs and issues in different repositories, so nothing shows the whole
queue at once. This script gathers it, read-only by default:

* **review-wait** — open, non-draft PRs whose checks are green but whose
  review is still required. Cross-account review needs the owner's fresh
  approval, so each one is an owner decision that is already ready.
* **owner-action** — open issues and PRs carrying the owner-action label
  (default ``owner-action``): real-device checks, secrets to enter, release
  sign-offs, product decisions.

Output is Markdown (default) or JSON (``--json``). ``--update-issue
OWNER/REPO#N`` rewrites only the block between the ``owner-queue`` markers in
that issue's body; without ``--apply`` it prints what it would write and
changes nothing. Sending the list anywhere else (a digest, an alert) is out of
scope here — that is a provider send and needs its own approval.

The repository owner (organisation) is fleet data, so it is never hardcoded:
pass ``--owner`` or set ``CCC_OWNER_QUEUE_OWNER``.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from typing import Callable, Sequence

START = "<!-- owner-queue:start -->"
END = "<!-- owner-queue:end -->"
FIELDS = "number,title,repository,url,createdAt,author"
LIMIT = "100"

Runner = Callable[[Sequence[str]], str]


def gh(args: Sequence[str]) -> str:
    """Run ``gh`` and return stdout; raise with stderr on failure."""
    proc = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:2])} failed: {proc.stderr.strip()[:300]}")
    return proc.stdout


def _parse_time(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def _item(raw: dict, kind: str, now: dt.datetime) -> dict:
    repo = raw.get("repository") or {}
    name = repo.get("nameWithOwner") or repo.get("name") or "?"
    created = _parse_time(raw["createdAt"])
    return {
        "kind": kind,
        "repo": name,
        "number": raw["number"],
        "title": raw.get("title", ""),
        "url": raw.get("url", ""),
        "author": (raw.get("author") or {}).get("login", "?"),
        "age_hours": max(0, int((now - created).total_seconds() // 3600)),
    }


_GREEN = {"SUCCESS", "NEUTRAL", "SKIPPED"}


def review_ready(view: dict) -> bool:
    """Green on every reported check, and neither approved nor change-requested.

    ``gh search prs --checks/--review`` cannot be trusted for this: on
    2026-11-01 ``--checks success`` matched 0 of 7 open non-draft PRs (it reads
    legacy commit statuses, not check runs) and ``--review required`` and
    ``--review none`` both matched the same 14. So every candidate is read
    with ``gh pr view``. An empty ``reviewDecision`` (repository without a
    required review) still counts: fleet policy needs the owner's approval for
    every merge, protected or not.
    """
    checks = view.get("statusCheckRollup") or []
    if not checks:
        return False
    for c in checks:
        state = (c.get("conclusion") or c.get("state") or "").upper()
        if state not in _GREEN:
            return False
    return (view.get("reviewDecision") or "") not in ("APPROVED", "CHANGES_REQUESTED")


def collect(owner: str, label: str, run: Runner = gh, now: dt.datetime | None = None) -> list[dict]:
    """Return owner-queue items, oldest first, de-duplicated by repo#number."""
    now = now or dt.datetime.now(dt.timezone.utc)
    candidates = json.loads(run([
        "search", "prs", "--owner", owner, "--state", "open", "--draft=false",
        "--json", FIELDS, "--limit", LIMIT,
    ]) or "[]")
    review = []
    for raw in candidates:
        repo = (raw.get("repository") or {}).get("nameWithOwner")
        if not repo:
            continue
        view = json.loads(run([
            "pr", "view", str(raw["number"]), "--repo", repo,
            "--json", "reviewDecision,statusCheckRollup",
        ]) or "{}")
        if review_ready(view):
            review.append(raw)
    labeled = json.loads(run([
        "search", "issues", "--owner", owner, "--state", "open", "--include-prs",
        "--label", label, "--json", FIELDS, "--limit", LIMIT,
    ]) or "[]")
    items: dict[tuple[str, int], dict] = {}
    for raw in review:
        it = _item(raw, "review-wait", now)
        items[(it["repo"], it["number"])] = it
    for raw in labeled:
        it = _item(raw, "owner-action", now)
        key = (it["repo"], it["number"])
        if key in items:  # labelled and review-ready: one row, both reasons
            items[key]["kind"] = "review-wait+owner-action"
        else:
            items[key] = it
    return sorted(items.values(), key=lambda i: (-i["age_hours"], i["repo"], i["number"]))


def _age(hours: int) -> str:
    return f"{hours // 24}d {hours % 24}h" if hours >= 24 else f"{hours}h"


def render(items: list[dict], now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now(dt.timezone.utc)
    kst = now.astimezone(dt.timezone(dt.timedelta(hours=9)))
    review = [i for i in items if i["kind"].startswith("review-wait")]
    action = [i for i in items if i["kind"] == "owner-action"]
    lines = [
        f"**오너 할 일 {len(items)}건** — 승인 대기 PR {len(review)} · owner-action {len(action)}"
        f" (갱신 {kst:%Y-%m-%d %H:%M} KST)",
        "",
    ]
    for title, group in (("승인 대기 PR (checks green · review required)", review),
                         ("owner-action 라벨", action)):
        lines.append(f"### {title}")
        if not group:
            lines.append("- 없음")
        for i in group:
            extra = " · 라벨도 있음" if i["kind"] == "review-wait+owner-action" else ""
            lines.append(f"- [ ] {i['repo']}#{i['number']} {i['title']} — {_age(i['age_hours'])}"
                         f" · @{i['author']}{extra} ({i['url']})")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def splice(body: str, block: str) -> str:
    """Replace the marker block in ``body`` (append one if absent)."""
    new = f"{START}\n{block.rstrip()}\n{END}"
    pattern = re.compile(re.escape(START) + r".*?" + re.escape(END), re.S)
    if pattern.search(body):
        return pattern.sub(lambda _m: new, body, count=1)
    return (body.rstrip() + "\n\n" if body.strip() else "") + new + "\n"


_TARGET = re.compile(r"^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([0-9]+)$")


def update_issue(target: str, block: str, apply: bool, run: Runner = gh) -> str:
    m = _TARGET.match(target)
    if not m:
        raise ValueError(f"--update-issue wants OWNER/REPO#N, got {target!r}")
    repo, number = m.group(1), m.group(2)
    body = json.loads(run(["issue", "view", number, "--repo", repo, "--json", "body"]))["body"] or ""
    new_body = splice(body, block)
    if new_body == body:
        return "unchanged"
    if not apply:
        return "would-update (dry-run; pass --apply to write)"
    run(["issue", "edit", number, "--repo", repo, "--body", new_body])
    return "updated"


def main(argv: Sequence[str] | None = None, run: Runner = gh) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--owner", default=os.environ.get("CCC_OWNER_QUEUE_OWNER", ""),
                    help="repository owner/organisation (or CCC_OWNER_QUEUE_OWNER)")
    ap.add_argument("--label", default=os.environ.get("CCC_OWNER_QUEUE_LABEL", "owner-action"))
    ap.add_argument("--json", action="store_true", help="print JSON instead of Markdown")
    ap.add_argument("--update-issue", metavar="OWNER/REPO#N",
                    help="rewrite the owner-queue block in this issue (dry-run unless --apply)")
    ap.add_argument("--apply", action="store_true", help="actually write --update-issue")
    args = ap.parse_args(argv)
    if not args.owner:
        print("ccc_owner_queue: --owner (or CCC_OWNER_QUEUE_OWNER) is required", file=sys.stderr)
        return 2
    try:
        items = collect(args.owner, args.label, run)
    except (RuntimeError, ValueError, KeyError) as exc:
        print(f"ccc_owner_queue: {exc}", file=sys.stderr)
        return 1
    block = render(items)
    if args.json:
        print(json.dumps({"owner": args.owner, "label": args.label, "items": items},
                         ensure_ascii=False, indent=2))
    else:
        print(block, end="")
    if args.update_issue:
        try:
            print(f"update-issue {args.update_issue}: {update_issue(args.update_issue, block, args.apply, run)}",
                  file=sys.stderr)
        except (RuntimeError, ValueError, KeyError) as exc:
            print(f"ccc_owner_queue: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
