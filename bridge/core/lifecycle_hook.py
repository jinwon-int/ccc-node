"""Claude hook → lifecycle ledger bridge (#645), fail-open CLI.

A Claude Code lifecycle hook can pipe its stdin JSON here to record a body-free
LifecycleObservation into the same owner-only ledger the Codex path uses, giving
the unified contract a Claude-side feed for events (prompt_submitted /
session_closed / provider_notification) that live only as shell hooks.

    <hook stdin JSON> | python3 -m telegram_bot.core.lifecycle_hook PostToolUse

Dependency-light (reads env, not the full bridge Settings) and always exits 0 —
observability must never fail a hook. No-op unless CCC_LIFECYCLE_AUDIT is set.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from typing import Any

from telegram_bot.core.lifecycle_audit import LifecycleAuditLedger
from telegram_bot.core.lifecycle_observation import normalize_claude_hook

_TRUE = {"1", "true", "yes", "on"}


def _enabled() -> bool:
    return os.environ.get("CCC_LIFECYCLE_AUDIT", "").strip().lower() in _TRUE


def _ledger_dir() -> Path:
    explicit = os.environ.get("CCC_LIFECYCLE_AUDIT_DIR")
    if explicit:
        return Path(explicit)
    state = os.environ.get("CCC_STATE_DIR") or (Path.home() / ".claude" / "state")
    return Path(state) / "lifecycle-audit"


def main(argv: list[str], stdin: Any = None) -> int:
    if not _enabled():
        return 0
    event = argv[1] if len(argv) > 1 else ""
    stream = stdin if stdin is not None else sys.stdin
    try:
        raw = stream.read() or "{}"
        payload = json.loads(raw)
    except Exception:
        return 0  # malformed input is a no-op, never a hook failure
    if not isinstance(payload, dict):
        return 0
    observation = normalize_claude_hook(event, payload)
    if observation is None:
        return 0
    try:
        LifecycleAuditLedger(_ledger_dir()).record(observation)
    except Exception:
        pass  # fail-open
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv))
