#!/usr/bin/env python3
"""Adapt one bridge distill-journal job into the payload nunchi ingests (#1018).

On bridge-managed nodes the bridge owns distill, so `claude/hooks/distill.sh`
exits early (`CCC_BRIDGE_DISTILL_MANAGED=1`) and never writes the
`distill-history` snapshots that `ingest-cron.sh` consumes. The extraction still
happens — it lands in the bridge's own journal — so the mirror reads it from
there instead of re-running an extractor. No provider call, no added cost.

Exit codes let the caller decide whether a job may be marked seen:
  0  payload written to stdout; ingest it
  1  usage / unreadable file
  3  nothing to ingest, and the job will never produce anything (terminal)
  4  nothing to ingest yet; the job is still in flight, so retry next tick
"""

from __future__ import annotations

import glob
import json
import os
import sys

EMITTED = 0
UNREADABLE = 1
TERMINAL_EMPTY = 3
IN_FLIGHT = 4

# A job in any other status may still reach extraction_done on a later tick.
_TERMINAL = frozenset({"extraction_done", "extraction_terminal_failed", "terminal_failed"})


def transcript_for(thread_id: str, projects_root: str | None = None) -> str:
    """Locate the SDK transcript for a bridge thread, '' when absent.

    nunchi's rank verification quotes the transcript to separate grounded facts
    from model paraphrase. The bridge journal has no path, but its `thread_id`
    is the SDK session id, so the project directory can be globbed instead of
    guessing the encoded cwd.
    """
    if not thread_id or "/" in thread_id or thread_id.startswith("."):
        return ""
    root = projects_root or os.path.expanduser("~/.claude/projects")
    hits = sorted(glob.glob(os.path.join(root, "*", f"{thread_id}.jsonl")))
    return hits[0] if hits else ""


def adapt(job: object, projects_root: str | None = None) -> dict | None:
    """Return the distill-history-shaped payload for a job, or None."""
    if not isinstance(job, dict):
        return None
    raw = job.get("extraction_output")
    if not raw:
        return None
    try:
        output = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return None
    if not isinstance(output, dict):
        return None
    items = output.get("honcho")
    if not isinstance(items, list) or not items:
        return None
    provenance = output.get("provenance")
    if not isinstance(provenance, dict):
        provenance = {}
    thread_id = str(job.get("thread_id") or "")
    payload = {
        "session_id": thread_id or str(job.get("job_id") or "unknown"),
        "distilled_at": provenance.get("distilled_at") or job.get("updated_at") or "",
        "trigger": provenance.get("trigger") or job.get("trigger") or "",
        "honcho": items,
    }
    transcript = transcript_for(thread_id, projects_root)
    if transcript:
        payload["transcript_path"] = transcript
    return payload


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: bridge-journal.py <journal-file>", file=sys.stderr)
        return UNREADABLE
    try:
        with open(argv[1], encoding="utf-8") as handle:
            job = json.load(handle)
    except (OSError, ValueError):
        return UNREADABLE
    payload = adapt(job)
    if payload is None:
        status = job.get("status") if isinstance(job, dict) else None
        return TERMINAL_EMPTY if status in _TERMINAL else IN_FLIGHT
    json.dump(payload, sys.stdout, ensure_ascii=False)
    return EMITTED


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
