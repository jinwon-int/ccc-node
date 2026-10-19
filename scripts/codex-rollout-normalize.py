#!/usr/bin/env python3
"""codex-rollout-normalize — project one Codex rollout into the Claude shape (#1353).

The skill-autosave drafting pipeline (skill-review.sh, scan.sh, extract.sh) is
built on the ~/.claude/projects/<encoded-cwd>/<session-id>.jsonl schema:
{"type":"user"|"assistant","message":{"role":…,"content":[…]}} lines. Codex
sessions live in a different tree ($CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl)
with a different record shape — {"timestamp","type","payload"} — so their
procedures never reach the drafting brain. This projector translates ONE rollout
file into that Claude shape so the entire downstream pipeline runs unmodified.

Since #2388 this script is a thin wrapper: record parsing and the mapping below
live in claude/hooks/lib/trajectory_render.py (codex_events → claude_rows), the
same adapter the prompt renderer uses, so projection and rendering cannot drift.
This file keeps the session metadata, exclusion, cap and file-layout policy.

Mapping (issue #1353, verified against live rollout samples):

  response_item / payload.type=="message" / role=="assistant"
      → {"type":"assistant","message":{"role":"assistant",
         "content":[{"type":"text","text":…}]}}            (output_text → text)
  response_item / payload.type=="message" / role=="user"
      → {"type":"user","message":{"role":"user",
         "content":[{"type":"text","text":…}]}}            (input_text → text)
  response_item / payload.type=="message" / role=="developer"|"system"
      → discarded — injected instruction blocks (skills_instructions,
        AGENTS.md, environment context), the same noise class the issue
        already discards for base_instructions
  response_item / payload.type=="function_call" (shell|exec_command|apply_patch)
      → {"type":"assistant","message":{"content":[{"type":"tool_use",
         "name":"Bash","input":{"command":…}}]}}
      — this exact shape is what scan.sh's Bash-shape extractor and the
        drafting extractors already consume, so they need zero changes.
        Other function_call names (wait, update_plan, view_image, …) carry
        no procedure and are discarded.
  event_msg / payload.type=="user_message"|"agent_message"
      → the same user/assistant text shapes — but ONLY as a file-level
        fallback: when the rollout carries no response_item message records
        at all (headless runs), these events are the only transcript of the
        conversation; when real messages exist, emitting both would duplicate
        every turn. Fallback rows keep session order relative to the command
        rows (before #2388 they were appended after all of them).
  session_meta / turn_context / world_state / token_count / event_msg(*) /
  reasoning / anything else
      → discarded (issue: noise and bulk — base_instructions alone is several
        KB of directives per session)

Safety limits:
  - --max-bytes (default 524288 = 512 KiB) caps the projected output per
    session; the projection stops at the cap and the summary reports
    "truncated": true. Downstream tail bounds (extract.sh 500 lines / 60 KiB)
    are unchanged and apply on top.
  - Sessions with originator=="codex_exec" are excluded by default (machine
    -driven runs must not self-reference into skill drafts — the same bias
    control as promotion's self-review ban). CCC_SKILL_CODEX_INCLUDE_EXEC=1
    / --include-exec lifts it.

Output: one JSON summary line on stdout:
  {"session_id","originator","cwd","project_enc","out_path","records_in",
   "records_out","out_bytes","truncated","excluded","empty","source_size"}
Exit codes: 0 = projected (including excluded/empty — they are outcomes),
1 = unreadable input, 2 = usage error.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys

MAX_OUT_BYTES_DEFAULT = 512 * 1024
PROJECT_ENC_RE = re.compile(r"[^A-Za-z0-9_]")


def _load_renderer():
    """Import the shared trajectory adapters (#2388): installed beside this
    script under hooks/lib/, or at claude/hooks/lib/ in a repo checkout."""
    here = os.path.dirname(os.path.abspath(__file__))
    for lib_dir in (os.path.join(here, "lib"), os.path.join(here, "..", "claude", "hooks", "lib")):
        path = os.path.join(lib_dir, "trajectory_render.py")
        if os.path.isfile(path):
            spec = importlib.util.spec_from_file_location("trajectory_render", path)
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = module  # importable by name (type introspection, pickling)
                spec.loader.exec_module(module)
                return module
    raise ImportError("trajectory_render.py not found beside the normalizer (hooks/lib/)")


tr = _load_renderer()


def encode_project_dir(cwd: str) -> str:
    """Same encoding as skill-review.sh's encode_project_dir()."""
    return PROJECT_ENC_RE.sub("-", cwd)


def read_records(source: str) -> list[dict]:
    """Parsed JSON-object lines of one rollout; malformed lines are skipped."""
    with open(source, encoding="utf-8", errors="replace") as fh:
        return list(tr.iter_records(fh))


def session_meta(records: list[dict]) -> dict[str, str]:
    """First non-empty session_id/cwd/originator from session_meta records."""
    meta: dict[str, str] = {"session_id": "", "cwd": "", "originator": ""}
    for record in records:
        payload = record.get("payload")
        if record.get("type") == "session_meta" and isinstance(payload, dict):
            for key in meta:
                value = payload.get(key)
                if value and not meta[key]:
                    meta[key] = str(value)
    return meta


def write_projection(rows: list[dict], out_path: str, max_out_bytes: int) -> tuple[int, int, bool]:
    """Write rows up to the cap; returns (out_bytes, records_out, truncated)."""
    out_bytes = 0
    truncated = False
    written: list[str] = []
    for row in rows:
        line = json.dumps(row, ensure_ascii=False)
        if out_bytes + len(line) + 1 > max_out_bytes:
            truncated = True
            break
        written.append(line)
        out_bytes += len(line) + 1
    tmp = f"{out_path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        for line in written:
            fh.write(line + "\n")
    os.replace(tmp, out_path)
    return out_bytes, len(written), truncated


def project_rollout(source: str, out_dir: str, max_out_bytes: int, include_exec: bool) -> dict:
    records = read_records(source)
    meta = session_meta(records)
    records_in = len(records)
    # Shared adapter (#2388): messages, command tool calls, and the file-level
    # event_msg fallback for headless rollouts, in session order.
    rows = tr.claude_rows(tr.codex_events(records))

    excluded = bool(meta["originator"] == "codex_exec" and not include_exec)
    project_enc = encode_project_dir(meta["cwd"]) if meta["cwd"] else "_unknown"
    session_slug = meta["session_id"] or os.path.basename(source).removesuffix(".jsonl")
    out_path = os.path.join(out_dir, project_enc, f"{session_slug}.jsonl")

    out_bytes = records_out = 0
    truncated = False
    if not excluded:
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        out_bytes, records_out, truncated = write_projection(rows, out_path, max_out_bytes)

    return {
        "session_id": meta["session_id"],
        "originator": meta["originator"],
        "cwd": meta["cwd"],
        "project_enc": project_enc,
        "out_path": out_path if not excluded else "",
        "records_in": records_in,
        "records_out": records_out,
        "out_bytes": out_bytes,
        "truncated": truncated,
        "excluded": excluded,
        "empty": records_out == 0,
        "source_size": os.path.getsize(source),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Project one Codex rollout jsonl into the Claude transcript shape (#1353)."
    )
    parser.add_argument("source", help="rollout-*.jsonl file to project")
    parser.add_argument(
        "--out-dir", required=True,
        help="root of the normalized tree (<out-dir>/<encoded-cwd>/<session-id>.jsonl)",
    )
    parser.add_argument(
        "--max-bytes", type=int, default=MAX_OUT_BYTES_DEFAULT,
        help=f"projected output cap per session (default {MAX_OUT_BYTES_DEFAULT})",
    )
    parser.add_argument(
        "--include-exec", action="store_true",
        help="project codex_exec sessions too (default: excluded — machine runs must not self-reference)",
    )
    args = parser.parse_args()

    if not os.path.isfile(args.source):
        print(f"source not found: {args.source}", file=sys.stderr)
        return 1
    if args.max_bytes < 1:
        print(f"--max-bytes must be >= 1, got {args.max_bytes}", file=sys.stderr)
        return 2

    try:
        summary = project_rollout(args.source, args.out_dir, args.max_bytes, args.include_exec)
    except OSError as exc:
        print(f"unreadable source: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
