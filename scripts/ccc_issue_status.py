#!/usr/bin/env python3
"""Issue status markers (#2914): parse, format and post ``status:v1`` lines.

A node that reports on a GitHub issue leaves one machine-readable first line,
in the same shape as the issue-claim marker (``issue-claim:v1``):

    <!-- status:v1 waiting=owner node=<node> due=2026-11-29T20:00 next="<one line>" -->

The most recent valid marker on an issue is its current state. The work board
(scripts/ccc-work-board.py) reads these lines to sort open issues into "owner's
turn", "waiting for a date", "node's turn" and "waiting on an outside party".

Fields:
  waiting  owner | node | date | external          (required)
  node     the node that owns the next step         (optional)
  due      YYYY-MM-DD or YYYY-MM-DDTHH:MM (KST)     (required when waiting=date)
  next     the next step, one line, <= 80 chars, double-quoted (optional)

Only the marker fields are ever read; the rest of a comment is ignored and never
stored. ``next`` is free text written by the node: it must not carry an owner's
message or a comment body.

CLI:
  ccc_issue_status.py format --waiting owner --node <node> --next "merge approval"
  ccc_issue_status.py set --repo OWNER/REPO --issue N --waiting date --due 2026-11-29T20:00 \
      [--node NODE] [--next TEXT] [--note TEXT] [--dry-run]
  ccc_issue_status.py show --repo OWNER/REPO --issue N
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional, Sequence

SCHEMA = "status:v1"
WAITING = ("owner", "node", "date", "external")
NEXT_MAX = 80
NODE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
DUE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2})?$")

# One marker per line, at the start of a line (a quoted example inside prose is
# not a marker). Fields are key=value or key="quoted value".
_MARKER_RE = re.compile(r"^<!--\s*status:v1\s+(?P<fields>.*?)\s*-->\s*$", re.M)
_FIELD_RE = re.compile(r'([a-z]+)=(?:"((?:[^"\\]|\\.)*)"|(\S+))')
_CLAIM_RE = re.compile(r"^<!--\s*issue-claim:v1\s+(?P<fields>.*?)\s*-->\s*$", re.M)


class StatusError(ValueError):
    """A marker that cannot be written or read as a valid status."""


@dataclass(frozen=True)
class Status:
    waiting: str
    node: str = ""
    due: str = ""
    next: str = ""

    def to_dict(self) -> dict:
        return {"waiting": self.waiting, "node": self.node, "due": self.due, "next": self.next}


def _fields(raw: str) -> dict:
    out = {}
    for key, quoted, bare in _FIELD_RE.findall(raw):
        value = bare if bare else re.sub(r"\\(.)", r"\1", quoted)
        out.setdefault(key, value)
    return out


def validate(waiting: str, node: str = "", due: str = "", next_text: str = "") -> Status:
    """Return a Status or raise StatusError with a short reason."""
    if waiting not in WAITING:
        raise StatusError(f"waiting must be one of {', '.join(WAITING)}")
    if node and not NODE_RE.match(node):
        raise StatusError("node must be a lowercase node name")
    if due:
        if not DUE_RE.match(due):
            raise StatusError("due must be YYYY-MM-DD or YYYY-MM-DDTHH:MM")
        try:
            datetime.strptime(due, "%Y-%m-%dT%H:%M" if "T" in due else "%Y-%m-%d")
        except ValueError:
            raise StatusError("due is not a real date") from None
    if waiting == "date" and not due:
        raise StatusError("waiting=date needs due")
    if "\n" in next_text or "\r" in next_text:
        raise StatusError("next must be one line")
    if len(next_text) > NEXT_MAX:
        raise StatusError(f"next must be at most {NEXT_MAX} characters")
    if "-->" in next_text:
        raise StatusError("next must not contain -->")
    return Status(waiting=waiting, node=node, due=due, next=next_text)


def format_marker(status: Status) -> str:
    parts = [f"waiting={status.waiting}"]
    if status.node:
        parts.append(f"node={status.node}")
    if status.due:
        parts.append(f"due={status.due}")
    if status.next:
        escaped = status.next.replace("\\", "\\\\").replace('"', '\\"')
        parts.append(f'next="{escaped}"')
    return f"<!-- {SCHEMA} {' '.join(parts)} -->"


def parse_body(body: str) -> tuple[Optional[Status], Optional[str]]:
    """Read the LAST status marker in one comment body.

    Returns (status, None) for a valid marker, (None, reason) for a marker that
    is present but invalid, and (None, None) when there is no marker at all.
    """
    matches = list(_MARKER_RE.finditer(body or ""))
    if not matches:
        return None, None
    fields = _fields(matches[-1].group("fields"))
    try:
        return validate(fields.get("waiting", ""), fields.get("node", ""), fields.get("due", ""),
                        fields.get("next", "")), None
    except StatusError as exc:
        return None, str(exc)


def parse_claim(body: str) -> Optional[dict]:
    """Read the LAST issue-claim marker in one comment body (node, session, state)."""
    matches = list(_CLAIM_RE.finditer(body or ""))
    if not matches:
        return None
    fields = _fields(matches[-1].group("fields"))
    return {k: fields.get(k, "") for k in ("node", "session", "state", "prev")}


def latest(comments: Iterable[dict]) -> dict:
    """Fold comments (oldest first: {created_at, body}) into the current state.

    Returns {"status", "status_at", "invalid", "invalid_at", "claim", "claim_at"}.
    A newer valid marker replaces an older one; an invalid marker newer than the
    last valid one is reported (``invalid``) but does not erase it. Bodies are
    read here and nowhere else; nothing from them is returned except marker fields.
    """
    state = {"status": None, "status_at": "", "invalid": "", "invalid_at": "", "claim": None, "claim_at": ""}
    for comment in comments:
        body = comment.get("body") or ""
        at = comment.get("created_at") or ""
        status, reason = parse_body(body)
        if status is not None:
            state.update(status=status.to_dict(), status_at=at, invalid="", invalid_at="")
        elif reason:
            state.update(invalid=reason, invalid_at=at)
        claim = parse_claim(body)
        if claim is not None:
            state.update(claim=claim, claim_at=at)
    return state


# ---------------------------------------------------------------------------- CLI

def _gh(gh_bin: str, args: Sequence[str], stdin: Optional[str] = None) -> str:
    result = subprocess.run([gh_bin, *args], input=stdin, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise StatusError(f"gh failed rc={result.returncode}: {result.stderr.strip()[:200]}")
    return result.stdout


def _repo(value: str) -> str:
    if not re.match(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$", value):
        raise argparse.ArgumentTypeError("want OWNER/REPO")
    return value


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="status:v1 issue markers (#2914)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("format", "set"):
        p = sub.add_parser(name)
        if name == "set":
            p.add_argument("--repo", type=_repo, required=True)
            p.add_argument("--issue", type=int, required=True)
            p.add_argument("--note", default="", help="a short human line under the marker")
            p.add_argument("--dry-run", action="store_true")
            p.add_argument("--gh-bin", default="gh")
        p.add_argument("--waiting", required=True, choices=WAITING)
        p.add_argument("--node", default="")
        p.add_argument("--due", default="")
        p.add_argument("--next", dest="next_text", default="")
    p = sub.add_parser("show")
    p.add_argument("--repo", type=_repo, required=True)
    p.add_argument("--issue", type=int, required=True)
    p.add_argument("--gh-bin", default="gh")
    args = ap.parse_args(argv)

    try:
        if args.cmd in ("format", "set"):
            marker = format_marker(validate(args.waiting, args.node, args.due, args.next_text))
            if args.cmd == "format":
                print(marker)
                return 0
            note = args.note.strip()
            if "\n" in note or len(note) > 200:
                raise StatusError("note must be one line of at most 200 characters")
            body = marker + ("\n" + note if note else "")
            if args.dry_run:
                print(json.dumps({"ok": True, "dry_run": True, "repo": args.repo, "issue": args.issue,
                                  "body": body}, ensure_ascii=False))
                return 0
            out = _gh(args.gh_bin, ["api", f"repos/{args.repo}/issues/{args.issue}/comments",
                                    "-f", f"body={body}", "--jq", ".html_url"])
            print(json.dumps({"ok": True, "url": out.strip()}, ensure_ascii=False))
            return 0
        raw = _gh(args.gh_bin, ["api", "--paginate", f"repos/{args.repo}/issues/{args.issue}/comments",
                                "--jq", ".[] | {created_at, body}"])
        comments = [json.loads(line) for line in raw.splitlines() if line.strip()]
        state = latest(comments)
        print(json.dumps({"ok": True, "repo": args.repo, "issue": args.issue, **state}, ensure_ascii=False))
        return 0
    except StatusError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
