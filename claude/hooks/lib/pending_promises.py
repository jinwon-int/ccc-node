#!/usr/bin/env python3
"""Render still-owed external-wait promises for SessionStart injection (#1258).

Why this exists
---------------
``ExternalWaitRegistry`` (#740) already survives a bridge restart: the
registry file is on disk and ``reconcile_on_start`` re-arms monitoring
records. What it does *not* do is tell the **next session** that a promise is
still outstanding. The registry is only readable by asking (``/waits``, the
CLI), and ``dropped_promises`` had no production caller at all -- so an agent
that said "I'll continue when CI goes green" and then lost its session to the
4-hour auto-new-session rotation or a self-update restart simply forgot.

That gap is what makes the promise look broken to the owner even though the
durable record was there the whole time.

This module is the read side of that record, shaped for a hook: stdlib only,
fail-open, and **silent when there is nothing owed** so the common case leaves
the loader's output byte-identical.

Two categories are reported, because they need different actions:

``monitoring``
    Still being polled. The agent must not re-register a duplicate wait for
    the same thing; it should just keep waiting.

``dropped``
    Terminal, the notification was delivered, but the continuation never ran
    (``wake.resumed is False``). Nothing else will pick these up -- the owner
    or the agent has to act by hand. ``skip_reason`` says why it stopped;
    ``session_moved`` is the one the 4-hour rotation produces.

Usage::

    python3 pending_promises.py [<waits.json path>] [--max-bytes N]

With no argument the path is resolved from ``CCC_EXTERNAL_WAIT_HOME``, which
the bridge already exports into hook subprocesses
(``bridge/utils/settings_memory.py``, #740).
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Tuple

# Keep the injected block small: this sits near the top of the memory payload,
# ahead of Honcho/Wiki, and the materializer truncates from the tail. A handful
# of promises is signal; a wall of them is noise that pushes out real context.
DEFAULT_MAX_BYTES = 1024
MAX_ROWS_PER_SECTION = 5

STATE_MONITORING = "monitoring"


def registry_path(argv: List[str]) -> str:
    """Resolve the waits.json path from argv, else CCC_EXTERNAL_WAIT_HOME."""
    positional = [a for a in argv if not a.startswith("--")]
    if positional:
        return positional[0]
    home = os.environ.get("CCC_EXTERNAL_WAIT_HOME", "").strip()
    if not home:
        return ""
    return os.path.join(home, "waits.json")


def _max_bytes(argv: List[str]) -> int:
    for i, arg in enumerate(argv):
        if arg == "--max-bytes" and i + 1 < len(argv):
            try:
                return max(0, int(argv[i + 1]))
            except (TypeError, ValueError):
                return DEFAULT_MAX_BYTES
        if arg.startswith("--max-bytes="):
            try:
                return max(0, int(arg.split("=", 1)[1]))
            except (TypeError, ValueError):
                return DEFAULT_MAX_BYTES
    return DEFAULT_MAX_BYTES


def load_records(path: str) -> List[Dict[str, Any]]:
    """Read the registry. Any problem yields no records -- never raises.

    A malformed or half-written registry must degrade to "nothing to say",
    exactly like the loader's other fail-open helpers. Losing the block is
    recoverable; a hook that dies takes the whole memory payload with it.
    """
    if not path:
        return []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    if isinstance(data, dict):
        values = data.values()
    elif isinstance(data, list):
        values = data
    else:
        return []
    return [rec for rec in values if isinstance(rec, dict)]


def classify(records: List[Dict[str, Any]]) -> Tuple[List[Dict], List[Dict]]:
    """Split into (still monitoring, promise dropped).

    Mirrors ``ExternalWaitRegistry.dropped_promises``: delivered notification
    (``wake.state == "done"``) plus ``resumed is False``. ``resumed`` missing
    is *not* a drop -- older records predate the field and guessing would
    manufacture alarms.
    """
    monitoring: List[Dict] = []
    dropped: List[Dict] = []
    for rec in records:
        wake = rec.get("wake")
        wake = wake if isinstance(wake, dict) else {}
        if rec.get("state") == STATE_MONITORING:
            monitoring.append(rec)
            continue
        if wake.get("state") == "done" and wake.get("resumed") is False:
            dropped.append(rec)
    key = lambda r: str(r.get("created_at") or "")  # noqa: E731
    return sorted(monitoring, key=key), sorted(dropped, key=key)


def _label(rec: Dict[str, Any]) -> str:
    repo = str(rec.get("repo") or "").strip()
    pr = rec.get("pr_number")
    sha = str(rec.get("head_sha") or "")[:8]
    # `pr` is compared against None rather than tested for truthiness: a falsy
    # check silently drops PR 0 and, worse, renders a bare repo that reads like
    # a different wait. Identity in this block has to stay exact.
    has_pr = pr is not None and str(pr).strip() != ""
    if repo and has_pr:
        head = f"{repo}#{pr}"
    else:
        head = repo or str(rec.get("wait_id") or "?")
    return f"{head} `{sha}`" if sha else head


def _row(rec: Dict[str, Any], *, with_reason: bool) -> str:
    # summary is already capped and body-free at registration (#740); no
    # transcript text can reach this block.
    summary = " ".join(str(rec.get("summary") or "").split())[:120]
    row = f"- {_label(rec)}"
    if summary:
        row += f" — {summary}"
    if with_reason:
        reason = str((rec.get("wake") or {}).get("skip_reason") or "").strip()
        if reason:
            row += f"  (skip: {reason})"
    wait_id = str(rec.get("wait_id") or "").strip()
    if wait_id:
        row += f"  [{wait_id}]"
    return row


def _section(title: str, rows: List[Dict], *, with_reason: bool) -> List[str]:
    if not rows:
        return []
    out = [title]
    for rec in rows[:MAX_ROWS_PER_SECTION]:
        out.append(_row(rec, with_reason=with_reason))
    hidden = len(rows) - MAX_ROWS_PER_SECTION
    if hidden > 0:
        out.append(f"- …외 {hidden}건 (`external_wait_cli list` 로 전체 확인)")
    return out


def render(records: List[Dict[str, Any]]) -> str:
    """Return the block body, or '' when nothing is owed."""
    monitoring, dropped = classify(records)
    if not monitoring and not dropped:
        return ""
    lines: List[str] = []
    lines += _section(
        "⏳ 아직 대기 중 — 중복 등록하지 말고 그대로 기다릴 것:",
        monitoring,
        with_reason=False,
    )
    if monitoring and dropped:
        lines.append("")
    lines += _section(
        "⚠ 알림은 갔으나 이어가지 못한 약속 — 아무도 대신 처리하지 않는다. 직접 확인할 것:",
        dropped,
        with_reason=True,
    )
    return "\n".join(lines)


def limit_bytes(text: str, cap: int) -> str:
    """Truncate on a UTF-8 boundary, mirroring memory_render.limit-bytes."""
    if cap <= 0:
        return text
    raw = text.encode("utf-8")
    if len(raw) <= cap:
        return text
    marker = "\n…(truncated)"
    room = max(0, cap - len(marker.encode("utf-8")))
    return raw[:room].decode("utf-8", "ignore") + marker


def main(argv: List[str]) -> int:
    try:
        body = render(load_records(registry_path(argv)))
    except Exception:  # noqa: BLE001 - fail open, never break the loader
        return 0
    if body:
        sys.stdout.write(limit_bytes(body, _max_bytes(argv)))
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
