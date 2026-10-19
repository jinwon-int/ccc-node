#!/usr/bin/env python3
"""piri-session-normalize — project one Piri (pi) session into the Claude shape.

The skill-autosave drafting pipeline (skill-review.sh, scan.sh, extract.sh) is
built on the ~/.claude/projects/<encoded-cwd>/<session-id>.jsonl schema:
{"type":"user"|"assistant","message":{"role":…,"content":[…]}} lines. Piri
sessions live in their own tree
($PIRI_CODING_AGENT_DIR/sessions/<encoded-cwd>/<ts>_<id>.jsonl) with a
different record shape, so their procedures never reach the drafting brain.
This projector translates ONE piri session file into the Claude shape so the
entire downstream pipeline runs unmodified — the same discipline the Codex
branch introduced (codex-rollout-normalize.py, #1353).

Since #2388 this script is a thin wrapper: record parsing and the mapping below
live in claude/hooks/lib/trajectory_render.py (piri_events → claude_rows), the
same adapter the prompt renderer uses. This file keeps the metadata, lock
probe, cap and file-layout policy.

Mapping (verified against live piri session samples, pi session version 3):

  {"type":"session"}                          → session metadata (id, cwd)
  {"type":"message","message":{"role":"user",
    "content":[{"type":"text",…}]}}           → {"type":"user",…} text rows
  {"type":"message","message":{"role":"assistant",
    "content":[{"type":"text",…}]}}           → {"type":"assistant",…} text rows
  {"type":"message","message":{"role":"assistant",
    "content":[{"type":"toolCall","name":"bash",
    "arguments":{"command":…}}]}}             → {"type":"assistant","message":{
    "content":[{"type":"tool_use","name":"Bash",
    "input":{"command":…}}]}}
      — this exact shape is what scan.sh's Bash-shape extractor and the
        drafting extractors already consume. Other toolCall names (read, edit,
        write, grep, …) carry no runnable command procedure and are discarded.
  assistant "thinking" items                  → discarded (reasoning noise,
        the same class as codex reasoning records)
  {"type":"message","message":{"role":"toolResult",…}} → discarded (tool
        output, not procedure)
  model_change / thinking_level_change / anything else → discarded

Safety limits:
  - --max-bytes (default 524288 = 512 KiB) caps the projected output per
    session; the projection stops at the cap and the summary reports
    "truncated": true. Downstream tail bounds (extract.sh 500 lines / 60 KiB)
    are unchanged and apply on top.
  - Piri sessions have no machine-originator marker to exclude (unlike codex
    rollouts' codex_exec): the summary always reports "excluded": false so the
    autosave branch logic stays uniform across providers.

Output: one JSON summary line on stdout:
  {"session_id","originator","cwd","project_enc","out_path","records_in",
   "records_out","out_bytes","truncated","excluded","empty","source_size"}
Exit codes: 0 = projected (including empty — an outcome), 1 = unreadable
input, 2 = usage error.
"""

from __future__ import annotations

import argparse
import fcntl
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


def rows_from_session(source: str) -> list[dict]:
    """Project message records: user/assistant text + bash tool calls, via the
    shared piri adapter (#2388)."""
    with open(source, encoding="utf-8", errors="replace") as fh:
        return tr.claude_rows(tr.piri_events(list(tr.iter_records(fh))))


def scan_meta(source: str) -> dict[str, str]:
    """Pass 1: session metadata from the session record."""
    meta: dict[str, str] = {"session_id": "", "cwd": ""}
    with open(source, encoding="utf-8", errors="replace") as fh:
        for record in tr.iter_records(fh):
            if record.get("type") == "session":
                for key in meta:
                    value = record.get(key)
                    if value and not meta[key]:
                        meta[key] = str(value)
                # pi names the session identifier "id", not "session_id".
                if not meta["session_id"]:
                    value = record.get("id")
                    if value:
                        meta["session_id"] = str(value)
                break
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


def project_session(source: str, out_dir: str, max_out_bytes: int, *, lock: bool = False,
                    project_enc_override: str = "") -> dict:
    meta = scan_meta(source)
    records_in = sum(1 for _ in open(source, encoding="utf-8", errors="replace"))

    # #1660: danso journals are flocked exclusively while the CLI writes; a
    # shared non-blocking probe fails closed there so the sweep defers the
    # session to a later run instead of projecting a partial transcript.
    # Piri sessions are unlocked, so the probe is a no-op for them.
    if lock:
        descriptor = os.open(source, os.O_RDONLY)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        except OSError:
            return {
                "session_id": meta["session_id"],
                "project_enc": project_enc_override,
                "records_in": records_in,
                "records_out": 0,
                "out_bytes": 0,
                "truncated": False,
                "excluded": False,
                "empty": True,
                "locked": True,
                "source_size": os.path.getsize(source),
            }
        finally:
            os.close(descriptor)

    rows = rows_from_session(source)
    if project_enc_override:
        project_enc = project_enc_override
    else:
        project_enc = encode_project_dir(meta["cwd"]) if meta["cwd"] else "_unknown"
    session_slug = meta["session_id"] or os.path.basename(source).removesuffix(".jsonl")
    out_path = os.path.join(out_dir, project_enc, f"{session_slug}.jsonl")

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    out_bytes, records_out, truncated = write_projection(rows, out_path, max_out_bytes)

    return {
        "session_id": meta["session_id"],
        "originator": "",
        "cwd": meta["cwd"],
        "project_enc": project_enc,
        "out_path": out_path,
        "records_in": records_in,
        "records_out": records_out,
        "out_bytes": out_bytes,
        "truncated": truncated,
        "excluded": False,
        "empty": records_out == 0,
        "locked": False,
        "source_size": os.path.getsize(source),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Project one Piri (pi) session jsonl into the Claude transcript shape."
    )
    parser.add_argument("source", help="pi session .jsonl file to project")
    parser.add_argument(
        "--out-dir", required=True,
        help="root of the normalized tree (<out-dir>/<encoded-cwd>/<session-id>.jsonl)",
    )
    parser.add_argument(
        "--max-bytes", type=int, default=MAX_OUT_BYTES_DEFAULT,
        help=f"projected output cap per session (default {MAX_OUT_BYTES_DEFAULT})",
    )
    parser.add_argument(
        "--project-enc", default="",
        help="override the encoded project directory (danso journals carry no "
        "encoded cwd; callers derive it from the journal root + audience scope)",
    )
    parser.add_argument(
        "--lock", action="store_true",
        help="probe a shared non-blocking flock and skip (empty + locked=true) "
        "when the writer holds the journal",
    )
    args = parser.parse_args()

    if not os.path.isfile(args.source):
        print(f"source not found: {args.source}", file=sys.stderr)
        return 1
    if args.max_bytes < 1:
        print(f"--max-bytes must be >= 1, got {args.max_bytes}", file=sys.stderr)
        return 2

    try:
        summary = project_session(
            args.source, args.out_dir, args.max_bytes,
            lock=args.lock,
            project_enc_override="".join(c for c in args.project_enc if c not in "/"),
        )
    except OSError as exc:
        print(f"unreadable source: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
