#!/usr/bin/env python3
"""codex-rollout-normalize — project one Codex rollout into the Claude shape (#1353).

The skill-autosave drafting pipeline (skill-review.sh, scan.sh, extract.sh) is
built on the ~/.claude/projects/<encoded-cwd>/<session-id>.jsonl schema:
{"type":"user"|"assistant","message":{"role":…,"content":[…]}} lines. Codex
sessions live in a different tree ($CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl)
with a different record shape — {"timestamp","type","payload"} — so their
procedures never reach the drafting brain. This projector translates ONE rollout
file into that Claude shape so the entire downstream pipeline runs unmodified.

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
        every turn.
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
import json
import os
import re
import sys

MAX_OUT_BYTES_DEFAULT = 512 * 1024
COMMAND_TOOLS = {"shell", "exec_command", "apply_patch"}
PROJECT_ENC_RE = re.compile(r"[^A-Za-z0-9_]")


def encode_project_dir(cwd: str) -> str:
    """Same encoding as skill-review.sh's encode_project_dir()."""
    return PROJECT_ENC_RE.sub("-", cwd)


def iter_records(fh):
    """Yield parsed JSON-object lines; malformed lines are skipped."""
    for line in fh:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            yield record


def content_items(payload: dict) -> list[dict[str, str]]:
    """Codex text content items → Claude {"type":"text","text":…} items."""
    items: list[dict[str, str]] = []
    content = payload.get("content")
    if not isinstance(content, list):
        return items
    for item in content:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            items.append({"type": "text", "text": item["text"]})
    return items


def command_string(name: str, arguments: object) -> str | None:
    """Extract the procedure payload from a codex function_call arguments blob."""
    if isinstance(arguments, str):
        try:
            args: object = json.loads(arguments)
        except ValueError:
            return None
    else:
        args = arguments
    if not isinstance(args, dict):
        return None
    if name in ("shell", "exec") and "command" in args:
        command = args["command"]
        if isinstance(command, list):
            parts = [str(part) for part in command]
            if len(parts) == 3 and parts[1] in ("lc", "-lc") and parts[0] in ("bash", "sh"):
                return parts[2]
            return " ".join(parts)
        if isinstance(command, str):
            return command
        return None
    if name == "exec_command":
        command = args.get("cmd", args.get("command"))
        if isinstance(command, list):
            return " ".join(str(part) for part in command)
        if isinstance(command, str):
            return command
        return None
    if name == "apply_patch":
        patch = args.get("input", args.get("patch"))
        return patch if isinstance(patch, str) else None
    return None


def message_row(role: str, payload: dict) -> dict:
    return {"type": role, "message": {"role": role, "content": content_items(payload)}}


def tool_use_row(command: str) -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "name": "Bash", "input": {"command": command}}],
        },
    }


def event_row(payload: dict, role: str) -> dict:
    return {
        "type": role,
        "message": {"role": role, "content": [{"type": "text", "text": payload.get("message")}]},
    }


def response_row(payload: dict) -> dict | None:
    """Map one response_item payload to a Claude row, or None to discard."""
    ptype = payload.get("type")
    if ptype == "message":
        role = payload.get("role")
        if role in ("assistant", "user"):
            return message_row(str(role), payload)
        return None  # developer/system: injected instruction noise
    if ptype == "function_call":
        name = payload.get("name")
        if name in COMMAND_TOOLS:
            command = command_string(str(name), payload.get("arguments"))
            if command:
                return tool_use_row(command)
    return None


def scan_meta_and_message_presence(source: str) -> tuple[dict[str, str], bool, int]:
    """Pass 1: session metadata, message presence, and record count."""
    meta: dict[str, str] = {"session_id": "", "cwd": "", "originator": ""}
    has_response_message = False
    records_in = 0
    with open(source, encoding="utf-8", errors="replace") as fh:
        for record in iter_records(fh):
            records_in += 1
            payload = record.get("payload")
            if record.get("type") == "response_item" and isinstance(payload, dict):
                if payload.get("type") == "message" and payload.get("role") in ("user", "assistant"):
                    has_response_message = True
            elif record.get("type") == "session_meta" and isinstance(payload, dict):
                for key in meta:
                    value = payload.get(key)
                    if value and not meta[key]:
                        meta[key] = str(value)
    return meta, has_response_message, records_in


def response_rows(source: str) -> list[dict]:
    """Pass 2: project response_item records (messages + command tool calls)."""
    rows: list[dict] = []
    with open(source, encoding="utf-8", errors="replace") as fh:
        for record in iter_records(fh):
            if record.get("type") != "response_item":
                continue
            payload = record.get("payload")
            if isinstance(payload, dict):
                row = response_row(payload)
                if row is not None:
                    rows.append(row)
    return rows


def event_fallback_rows(source: str) -> list[dict]:
    """File-level fallback (#1353): headless rollouts record the conversation
    only as event_msg; project those when real response_item messages are absent."""
    rows: list[dict] = []
    with open(source, encoding="utf-8", errors="replace") as fh:
        for record in iter_records(fh):
            if record.get("type") != "event_msg":
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            ptype = payload.get("type")
            if ptype in ("user_message", "agent_message") and isinstance(
                payload.get("message"), str
            ):
                rows.append(event_row(payload, "user" if ptype == "user_message" else "assistant"))
    return rows


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
    meta, has_response_message, records_in = scan_meta_and_message_presence(source)

    rows = response_rows(source)
    if not has_response_message:
        rows.extend(event_fallback_rows(source))

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
