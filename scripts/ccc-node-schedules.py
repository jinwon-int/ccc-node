#!/usr/bin/env python3
"""Read-only list of this node's scheduled checks, as one JSON document (#2914 stage 3).

The work board's hub pipes this file to each node over SSH
(``ssh <node> python3 - --node <node> < scripts/ccc-node-schedules.py``), so a
node needs nothing installed and always runs the hub's version.

What is read, and the only fields kept:

- detached-jobs registry (``$CCC_STATE_DIR/detached-jobs.jsonl``): scheduled,
  not yet acknowledged rows — unit, scheduled time, summary;
- systemd timers (system and, when present, user): timer unit, next run,
  whether it is transient (lost on reboot, #2827), its Description;
- crontab: the schedule fields, the command's script basename, and a trailing
  ``# comment`` — never the command line, its arguments or environment;
- agent-cron tasks: id, name, schedule, enabled — never the prompt.

Free text (summaries, descriptions, comments) is cut to one line of at most
120 characters. Issue references written as ``repo#123`` or ``#123`` are
pulled out so the board can match them to issues. Nothing is written.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional

SCHEMA = "ccc.node-schedules.v1"
TEXT_MAX = 120
REF_RE = re.compile(r"(?<![\w/])(?:([A-Za-z0-9][A-Za-z0-9._-]{0,99})#|#)(\d{1,6})\b")
SAFE_NAME = re.compile(r"[^A-Za-z0-9._@:-]+")
CRON_MACROS = ("@reboot", "@yearly", "@annually", "@monthly", "@weekly", "@daily", "@midnight", "@hourly")


def clean_text(value: object) -> str:
    text = " ".join(str(value or "").split())
    text = "".join(ch for ch in text if ch.isprintable())
    return text if len(text) <= TEXT_MAX else text[: TEXT_MAX - 1] + "…"


def clean_name(value: object) -> str:
    return SAFE_NAME.sub("-", str(value or "")).strip("-")[:100]


def issue_refs(*texts: str) -> List[dict]:
    seen, out = set(), []
    for text in texts:
        for repo, number in REF_RE.findall(text or ""):
            key = (repo, int(number))
            if key not in seen:
                seen.add(key)
                out.append({"repo": repo, "number": int(number)})
    return out


def _item(source: str, name: str, next_at: str = "", schedule: str = "", summary: str = "",
          persistent: Optional[bool] = None, enabled: bool = True) -> dict:
    summary = clean_text(summary)
    name = clean_name(name)
    return {"source": source, "name": name, "next_at": next_at, "schedule": clean_text(schedule),
            "summary": summary, "persistent": persistent, "enabled": enabled,
            "issues": issue_refs(name, summary)}


# ------------------------------------------------------------------ detached jobs

def detached_jobs(state_dir: Path) -> List[dict]:
    path = state_dir / "detached-jobs.jsonl"
    rows: dict = {}
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and rec.get("unit"):
                    rows.setdefault(rec["unit"], {}).update(rec)
    except FileNotFoundError:
        return []
    out = []
    for rec in rows.values():
        if not rec.get("scheduled_at") or rec.get("acked"):
            continue
        out.append(_item("detached-job", rec["unit"], next_at=str(rec.get("scheduled_at") or ""),
                         summary=rec.get("summary") or "", persistent=False))
    return out


# ------------------------------------------------------------------ systemd

def _iso_from_usec(usec: object) -> str:
    try:
        value = int(usec)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    return datetime.fromtimestamp(value / 1_000_000, timezone.utc).isoformat(timespec="seconds")


def _run(argv: List[str], timeout: int = 20) -> Optional[str]:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _descriptions(scope: List[str], units: List[str]) -> dict:
    if not units:
        return {}
    raw = _run(["systemctl", *scope, "show", "-p", "Id", "-p", "Description", "--", *units]) or ""
    out, cur = {}, {}
    for line in raw.splitlines() + [""]:
        if not line.strip():
            if cur.get("Id"):
                out[cur["Id"]] = cur.get("Description", "")
            cur = {}
            continue
        key, _, value = line.partition("=")
        cur[key] = value
    return out


def systemd_timers(transient_dirs: Iterable[Path]) -> List[dict]:
    if not shutil.which("systemctl"):
        return []
    transient = set()
    for d in transient_dirs:
        try:
            transient.update(p.name for p in d.iterdir() if p.name.endswith(".timer"))
        except OSError:
            pass
    out = []
    for scope in ([], ["--user"]):
        raw = _run(["systemctl", *scope, "list-timers", "--all", "--output=json", "--no-pager"])
        if not raw:
            continue
        try:
            timers = json.loads(raw)
        except ValueError:
            continue
        names = [t.get("unit") for t in timers if isinstance(t, dict) and t.get("unit")]
        desc = _descriptions(scope, names)
        for t in timers:
            unit = (t or {}).get("unit")
            if not unit:
                continue
            out.append(_item("systemd-user" if scope else "systemd", unit, next_at=_iso_from_usec(t.get("next")),
                             summary=desc.get(unit, ""), persistent=unit not in transient))
    return out


# ------------------------------------------------------------------ crontab

_WRAPPERS = {"env", "nice", "ionice", "flock", "timeout", "bash", "sh", "python", "python3", "node", "cd", "exec"}


def _script_name(tokens: List[str]) -> str:
    """The basename of the script a cron line runs — nothing else from the line.

    Only a token naming a script file (``*.sh``, ``*.py`` …) or an executable
    path (contains ``/``) qualifies, and never one with ``=`` in it, so an
    environment assignment or a ``--flag=value`` can never become the name."""
    words, skip = [], False
    for tok in tokens:
        word = tok.strip("\"'")
        if not skip:
            words.append(word)
        skip = word == "cd"  # the directory after cd is not what runs
    words = [w for w in words if w and "=" not in w and "://" not in w and not w.startswith("-") and "$" not in Path(w).name
             and not re.search(r"\.(log|txt|json|jsonl|env|lock|md)$", w)]
    for w in words:
        if re.search(r"\.(sh|py|mjs|js|pl|rb)$", w):
            return Path(w).name
    for w in words:
        base = Path(w).name
        if "/" in w and base not in _WRAPPERS and re.fullmatch(r"[A-Za-z0-9._-]+", base):
            return base
    return ""


def crontab_lines(text: str) -> List[dict]:
    out = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", stripped):
            continue  # a crontab environment line
        comment = ""
        if " # " in stripped:
            stripped, _, comment = stripped.partition(" # ")
        fields = stripped.split()
        if fields and fields[0] in CRON_MACROS:
            schedule, rest = fields[0], fields[1:]
        elif len(fields) >= 6:
            schedule, rest = " ".join(fields[:5]), fields[5:]
        else:
            continue
        out.append(_item("cron", _script_name(rest) or "command", schedule=schedule, summary=comment,
                         persistent=True))
    return out


def crontab() -> List[dict]:
    if not shutil.which("crontab"):
        return []
    return crontab_lines(_run(["crontab", "-l"]) or "")


# ------------------------------------------------------------------ agent-cron

def agent_cron(state_dir: Path) -> List[dict]:
    try:
        doc = json.loads((state_dir / "agent-cron" / "tasks.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for task in (doc.get("tasks") if isinstance(doc, dict) else None) or []:
        if not isinstance(task, dict):
            continue
        out.append(_item("agent-cron", task.get("id") or "", schedule=task.get("schedule") or "",
                         summary=task.get("name") or "", persistent=True, enabled=bool(task.get("enabled"))))
    return out


# ------------------------------------------------------------------ main

def collect(node: str, state_dir: Path, transient_dirs: Iterable[Path], now: Optional[datetime] = None) -> dict:
    items: List[dict] = []
    errors: List[str] = []
    for name, fn in (("detached-jobs", lambda: detached_jobs(state_dir)),
                     ("systemd", lambda: systemd_timers(transient_dirs)),
                     ("crontab", crontab),
                     ("agent-cron", lambda: agent_cron(state_dir))):
        try:
            items.extend(fn())
        except Exception as exc:  # one broken source must not hide the others
            errors.append(f"{name}: {type(exc).__name__}")
    now = now or datetime.now(timezone.utc)
    return {"schema": SCHEMA, "node": clean_name(node), "generated_at": now.isoformat(timespec="seconds"),
            "items": items, "errors": errors}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--node", default=os.environ.get("CCC_NODE_NAME", ""))
    ap.add_argument("--state-dir", default=os.environ.get("CCC_STATE_DIR", str(Path.home() / ".claude/state")))
    args = ap.parse_args(argv)
    doc = collect(args.node or os.uname().nodename, Path(args.state_dir),
                  [Path("/run/systemd/transient"), Path(f"/run/user/{os.getuid()}/systemd/transient")])
    sys.stdout.write(json.dumps(doc, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
