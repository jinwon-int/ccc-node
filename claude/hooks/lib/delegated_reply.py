#!/usr/bin/env python3
"""Classify the owner's reply to an open delegated-approval ask (ccc-node#2137, spec §10.2 5-d slice 3).

Called by ``delegated-reply.sh`` (UserPromptSubmit) with the hook payload on stdin. It looks at
three things, all in memory: the prompt the owner just sent, the last ``[답변 필요]`` block the
agent wrote (the tail of the session transcript), and this node's open asks in the shadow ledger.
When the reply is a clear non-approval it appends one ``decision`` row through the recorder.

Rules — when in doubt, it is not a refusal:

- the reply carries the PR's approval phrase (``<n> 승인``), or picks the option that does
  → nothing; the approval tool records the approval.
- the block is single-choice, one of its options carries the approval phrase, and the reply picks
  a *different* option of that block → ``decline``.
- the reply says hold / 보류 / 중단 together with the PR number → ``hold``.
- the block is multi-choice and the approval option was not picked, or the reply is free text
  while the approval option was on the table → ``reply-unclassified`` (a note, not a refusal).
- two open asks share the PR number (different repositories) → ``reply-unclassified``.
- anything unrelated to an open ask → nothing.
- a prompt the harness produced (an external-event wake-up, a task notification) → nothing.

Nothing textual is stored: the row holds the class, the source ``reply``, the PR and the head.
Never prints; the caller discards output and always exits 0.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

TAIL_BYTES = 400_000
MARKER_OPEN = "open-asks"
HEADER_RE = re.compile(r"^\s*\*{0,2}\[(?:답변 필요|Answer needed)\]\*{0,2}\s*(.*)$")
OPTION_RE = re.compile(r"^\s*(\d{1,2})[.)]\s+(.*\S)\s*$")
HOLD_RE = re.compile(r"(?<![a-z])hold(?![a-z])|보류|중단", re.I)
BARE_NUMBERS_RE = re.compile(r"^[\s\d,.·/&+]+$")
# Turns the harness starts by itself (a CI or queue wake-up, a background-task notice) arrive as
# prompts too. They are not the owner answering, so they are never classified.
MACHINE_PROMPT_RE = re.compile(r"^\s*(?:\[external_event:|<system-reminder>|<task-notification>|\[SYSTEM NOTIFICATION)", re.I)


def norm(text: str) -> str:
    """Comparable form: no markdown marks, no recommendation tag, single spaces, lower case."""
    text = re.sub(r"[`*_]", "", text)
    text = re.sub(r"\((?:권장|recommended)\)", "", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip().lower()


def find_recorder():
    """The shadow recorder module from the managed gh-pr-flow skill, or None when it is not installed."""
    here = Path(__file__).resolve()
    roots = [os.environ.get("CCC_DELEGATED_RECORDER_DIR"),
             os.path.join(os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex"),
                          "skills", "gh-pr-flow", "scripts"),
             str(here.parents[3] / "codex" / "skills" / "gh-pr-flow" / "scripts") if len(here.parents) > 3 else None]
    for root in roots:
        path = Path(root) / "delegated_approval_shadow.py" if root else None
        if path and path.is_file():
            spec = importlib.util.spec_from_file_location("delegated_approval_shadow", path)
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                return module
    return None


def last_assistant_text(transcript_path: Optional[str]) -> str:
    """The newest main-thread assistant text in the transcript tail; empty when it cannot be read."""
    if not transcript_path:
        return ""
    try:
        with open(transcript_path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - TAIL_BYTES))
            lines = fh.read().decode("utf-8", "replace").split("\n")
    except OSError:
        return ""
    for line in reversed(lines):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("type") != "assistant" or record.get("isSidechain"):
            continue
        content = (record.get("message") or {}).get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "\n".join(str(part.get("text") or "") for part in content
                             if isinstance(part, dict) and part.get("type") == "text")
        else:
            text = ""
        if text.strip():
            return text
    return ""


def last_block(text: str) -> Optional[dict]:
    """``{"mode": "one"|"many", "options": [normalised text, …]}`` for the last answer-needed block."""
    block = None
    lines = text.split("\n")
    for i, line in enumerate(lines):
        header = HEADER_RE.match(line)
        if not header:
            continue
        rest = header.group(1)
        mode = "many" if ("여러" in rest or "choose any" in rest.lower()) else "one"
        options: list[str] = []
        for follow in lines[i + 1:]:
            option = OPTION_RE.match(follow)
            if option:
                options.append(norm(option.group(2)))
            elif follow.strip() or options:  # the list ends at the first other line, or at a blank after it began
                break
        if options:
            block = {"mode": mode, "options": options}
    return block


def selected_options(prompt: str, options: list[str]) -> set[int]:
    """Indexes of the options the reply picks: sent back verbatim, as ``N. text`` lines, or as bare numbers."""
    picked: set[int] = set()
    flat = norm(prompt)
    if BARE_NUMBERS_RE.match(prompt):
        for number in re.findall(r"\d{1,2}", prompt):
            if 1 <= int(number) <= len(options):
                picked.add(int(number) - 1)
        return picked
    for index, option in enumerate(options):
        if len(option) >= 6 and option in flat:
            picked.add(index)
    for line in prompt.split("\n"):
        numbered = OPTION_RE.match(line)
        if numbered and 1 <= int(numbered.group(1)) <= len(options):
            index, body = int(numbered.group(1)) - 1, norm(numbered.group(2))
            if body and (options[index].startswith(body[:24]) or body.startswith(options[index][:24])):
                picked.add(index)
    return picked


def approves(text_norm: str, pr: int) -> bool:
    return bool(re.search(rf"(?<!\d){pr}\s*(?:외부\s*)?승인", text_norm))


def mentions(text: str, pr: int) -> bool:
    return bool(re.search(rf"(?<!\d){pr}(?!\d)", text))


def classify(prompt: str, block: Optional[dict], pr: int) -> Optional[tuple[str, str]]:
    """``(decision, detail)`` for one open ask, or None when nothing should be recorded."""
    flat = norm(prompt)
    if approves(flat, pr):
        return None
    options = block["options"] if block else []
    approve_at = {i for i, option in enumerate(options) if approves(option, pr)}
    picked = selected_options(prompt, options) if options else set()
    if picked & approve_at:
        return None
    if HOLD_RE.search(prompt) and mentions(prompt, pr):
        return "hold", "hold-word"
    if not approve_at:
        return None  # the approval was not on the table: unrelated to this ask
    if picked and block["mode"] == "one":
        return "decline", "other-option"
    if picked:
        return "reply-unclassified", "multi-choice-not-picked"
    return "reply-unclassified", "free-text"


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    prompt = str(payload.get("prompt") or payload.get("user_prompt") or "") if isinstance(payload, dict) else ""
    if not prompt.strip() or MACHINE_PROMPT_RE.match(prompt):
        return 0
    shadow = find_recorder()
    if shadow is None:
        return 0
    state_dir = Path(os.environ["CCC_STATE_DIR"]) if os.environ.get("CCC_STATE_DIR") else None
    path = shadow.ledger_path(state_dir)
    rows = shadow.read_ledger(path)
    now = time.time()
    open_keys = [key for key, how in shadow.resolve_asks(rows, until=now + 1).items() if how == "open"]
    if not open_keys:
        try:
            (path.parent / MARKER_OPEN).unlink()
        except OSError:
            pass
        return 0
    text = last_assistant_text(payload.get("transcript_path"))
    block = last_block(text) if text else None
    by_pr: dict = {}
    for repo, pr, head in open_keys:
        by_pr.setdefault(pr, []).append((repo, head))
    for pr, targets in by_pr.items():
        verdict = classify(prompt, block, int(pr))
        if verdict is None:
            continue
        if len(targets) > 1:
            verdict = ("reply-unclassified", "ambiguous-pr")
        for repo, head in targets:
            shadow.append_ledger(path, shadow.build_decision_row(
                repo=repo, pr=pr, head=head, decision=verdict[0], source="reply", detail=verdict[1], now=now))
    return 0


if __name__ == "__main__":
    try:
        main()
    except Exception:  # fail-open: this hook must never disturb a turn
        pass
    raise SystemExit(0)
