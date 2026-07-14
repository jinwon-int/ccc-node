"""Render the operator-facing ``--status`` view from a health.json snapshot.

Single source for the health rendering that ``start.sh``'s ``--status`` shows —
staleness threshold, component state → icon mapping, and elapsed-time formatting
(#455). Previously this lived as a ~100-line embedded ``python3 - <<'PY'``
heredoc in start.sh, which could not be imported, tested, or type-checked, so it
drifted silently from ``utils/health.py`` (the writer of the same schema).

Self-contained (standard library only) so start.sh can run it with the system
``python3`` even when the venv is unavailable, exactly like the old heredoc:

    python3 bridge/utils/health_render.py <health.json> <pid> <stale_s> <provider>

and importable for tests via ``render_status_lines``.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

_ICONS = {
    "available": "🟢",
    "starting": "🟡",
    "degraded": "🟡",
    "unavailable": "🔴",
}


def _agent_label(provider: str) -> str:
    return "Codex" if str(provider).strip().lower() == "codex" else "Claude"


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _format_age(seconds: int) -> str:
    if seconds >= 3600:
        return f"{seconds // 3600}h"
    if seconds >= 60:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def _line(component: str, state: str, detail: str = "") -> str:
    if detail:
        return f"   {component}: {state} ({detail})"
    return f"   {component}: {state}"


def render_status_lines(
    health_path: Path,
    pid: str,
    stale_seconds: int,
    configured_provider: str,
    *,
    now: Optional[datetime] = None,
) -> List[str]:
    """Return the ``--status`` lines for a health snapshot (byte-identical to the
    former start.sh heredoc). ``now`` is injectable for deterministic tests."""
    configured_label = _agent_label(configured_provider)

    if not health_path.exists():
        return [
            "🟡 Bot status: degraded",
            _line("Process", "alive", f"PID: {pid}"),
            _line("Service", "degraded", "health missing"),
            _line("Telegram", "degraded", "health missing"),
            _line(configured_label, "degraded", "health missing"),
        ]

    try:
        data = json.loads(health_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return [
            "🟡 Bot status: degraded",
            _line("Process", "alive", f"PID: {pid}"),
            _line("Service", "degraded", f"invalid health file: {exc}"),
            _line("Telegram", "degraded", "health unreadable"),
            _line(configured_label, "degraded", "health unreadable"),
        ]

    updated_at = _parse_iso(data.get("updated_at"))
    age_seconds = None
    if updated_at is not None:
        reference = now or datetime.now(timezone.utc)
        age_seconds = max(0, int((reference - updated_at).total_seconds()))

    service = data.get("service") or {}
    telegram = data.get("telegram") or {}
    agent = data.get("agent") or data.get("claude") or {}
    provider = str(agent.get("provider") or configured_provider).strip().lower()
    agent_label = "Codex" if provider == "codex" else "Claude"

    if age_seconds is None or age_seconds > stale_seconds:
        detail = "health stale"
        if age_seconds is not None:
            detail = f"health stale: last update {_format_age(age_seconds)} ago"
        return [
            "🟡 Bot status: degraded",
            _line("Process", "alive", f"PID: {pid}"),
            _line("Service", "degraded", detail),
            _line("Telegram", "degraded", detail),
            _line(agent_label, "degraded", detail),
        ]

    service_state = service.get("state") or "degraded"
    service_reason = service.get("reason") or ""
    telegram_state = telegram.get("state") or "degraded"
    telegram_reason = telegram.get("last_error") or ""
    agent_state = agent.get("state") or "degraded"
    agent_reason = agent.get("last_error") or ""

    return [
        f"{_ICONS.get(service_state, '🟡')} Bot status: {service_state}",
        _line("Process", "alive", f"PID: {pid}"),
        _line("Service", service_state, service_reason),
        _line(
            "Telegram",
            telegram_state,
            telegram_reason if telegram_state != "healthy" else "",
        ),
        _line(
            agent_label,
            agent_state,
            agent_reason if agent_state != "healthy" else "",
        ),
    ]


def main(argv: List[str]) -> int:
    if len(argv) < 4:
        print("usage: health_render.py <health.json> <pid> <stale_s> <provider>", file=sys.stderr)
        return 2
    lines = render_status_lines(
        Path(argv[0]), argv[1], int(argv[2]), argv[3]
    )
    for entry in lines:
        print(entry)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
