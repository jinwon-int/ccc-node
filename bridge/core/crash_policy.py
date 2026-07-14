"""Canonical crash / rapid-restart policy loader (#445).

The bridge has two deliberately nested restart guards — the in-process polling
rebuild (``core/bot_lifecycle.py``) and the process supervisor
(``start.sh run_daemon_supervisor``). They previously hard-coded their crash
windows and strike counts independently, so the effective back-off/give-up
policy was an undocumented *product* of the two and tuning one silently diverged
from the other.

This module makes ``bridge/crash-policy.env`` the single source both runtimes
read. Resolution precedence per key:

    1. process environment  (the supervisor sources crash-policy.env and exports
       these, so a bash-launched child matches whatever the supervisor used)
    2. bridge/crash-policy.env  (the canonical file; also what the supervisor
       sources)
    3. the documented ``_DEFAULTS`` mirror below — a robustness fallback for
       when the file is unreadable, kept in sync with crash-policy.env and
       asserted equal by test_crash_policy.

See crash-policy.env for the two-layer design rationale (why the windows differ
and why the in-process give-up is intentionally recounted as one process crash).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict

POLICY_FILE = Path(__file__).resolve().parents[1] / "crash-policy.env"

#: Fallback mirror of crash-policy.env. Only used when the file is unreadable and
#: the supervisor injected no environment. Kept in sync with the file (drift is
#: asserted by test_crash_policy.test_defaults_match_policy_file).
_DEFAULTS: Dict[str, int] = {
    "CCC_MAX_RAPID_CRASHES": 5,
    "CCC_PROCESS_CRASH_WINDOW_SECONDS": 60,
    "CCC_INPROCESS_MIN_UPTIME_SECONDS": 30,
    "CCC_RESTART_DELAY_BASE_SECONDS": 3,
    "CCC_RESTART_DELAY_MAX_SECONDS": 30,
}


def parse_env_file(path: Path) -> Dict[str, str]:
    """Parse a shell-sourceable KEY=VALUE file (the subset crash-policy.env uses)."""
    values: Dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, raw = stripped.partition("=")
        values[key.strip()] = raw.strip().strip('"').strip("'")
    return values


def _resolve() -> Dict[str, int]:
    file_values = parse_env_file(POLICY_FILE)
    resolved: Dict[str, int] = {}
    for key, default in _DEFAULTS.items():
        raw = os.environ.get(key)
        if raw is None:
            raw = file_values.get(key)
        try:
            resolved[key] = (
                int(raw) if raw is not None and str(raw).strip() != "" else default
            )
        except (TypeError, ValueError):
            resolved[key] = default
    return resolved


_POLICY = _resolve()

#: Shared strike count for BOTH guards.
MAX_RAPID_CRASHES: int = _POLICY["CCC_MAX_RAPID_CRASHES"]
#: In-process guard: a rebuilt polling loop dying within this many seconds is rapid.
INPROCESS_MIN_UPTIME_SECONDS: int = _POLICY["CCC_INPROCESS_MIN_UPTIME_SECONDS"]
#: Process supervisor: a process dying within this many seconds is rapid.
PROCESS_CRASH_WINDOW_SECONDS: int = _POLICY["CCC_PROCESS_CRASH_WINDOW_SECONDS"]
#: Process supervisor back-off base / cap (seconds).
RESTART_DELAY_BASE_SECONDS: int = _POLICY["CCC_RESTART_DELAY_BASE_SECONDS"]
RESTART_DELAY_MAX_SECONDS: int = _POLICY["CCC_RESTART_DELAY_MAX_SECONDS"]
