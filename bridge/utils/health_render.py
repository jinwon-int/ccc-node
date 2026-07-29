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
import math
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
    except (AttributeError, TypeError, ValueError):
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


def _turn_occupancy_line(
    data: dict,
    *,
    reference: datetime,
    stale_seconds: int,
) -> str:
    workload = data.get("workload")
    if not isinstance(workload, dict):
        return _line("Turn occupancy", "unknown", "not reported")

    occupancy = workload.get("turn_occupancy")
    if not isinstance(occupancy, dict):
        return _line("Turn occupancy", "unknown", "not reported")

    observed_at = _parse_iso(occupancy.get("observed_at"))
    if observed_at is None or observed_at.tzinfo is None:
        return _line("Turn occupancy", "unknown", "observation time unavailable")
    observation_age = max(
        0,
        int(
            (
                reference - observed_at.astimezone(timezone.utc)
            ).total_seconds()
        ),
    )
    if observation_age > stale_seconds:
        return _line(
            "Turn occupancy",
            "unknown",
            f"workload stale: last observation {_format_age(observation_age)} ago",
        )

    state = occupancy.get("state")
    active_requests = workload.get("active_requests")
    active_count = (
        active_requests
        if isinstance(active_requests, int)
        and not isinstance(active_requests, bool)
        and active_requests >= 0
        else None
    )
    waiting_for_turn = workload.get("waiting_for_turn")
    waiting_count = (
        waiting_for_turn
        if isinstance(waiting_for_turn, int)
        and not isinstance(waiting_for_turn, bool)
        and waiting_for_turn >= 0
        else None
    )
    if state == "idle":
        if active_count != 0:
            return _line("Turn occupancy", "unknown", "inconsistent active turn count")
        if waiting_count is not None and waiting_count != 0:
            return _line("Turn occupancy", "unknown", "inconsistent waiting turn count")
        detail = "0 waiting for runtime admission" if waiting_count == 0 else ""
        return _line("Turn occupancy", "idle", detail)
    if state != "occupied":
        return _line("Turn occupancy", "unknown", "invalid state")
    if active_count is None or active_count == 0:
        return _line("Turn occupancy", "unknown", "inconsistent active turn count")
    if waiting_count is not None and waiting_count > active_count:
        return _line("Turn occupancy", "unknown", "inconsistent waiting turn count")

    details: List[str] = []
    noun = "turn" if active_count == 1 else "turns"
    details.append(f"{active_count} active {noun}")
    if waiting_count is not None:
        details.append(f"{waiting_count} waiting for runtime admission")
    else:
        details.append("admission wait unavailable")

    oldest_turn_started_at = _parse_iso(occupancy.get("oldest_turn_started_at"))
    if (
        oldest_turn_started_at is not None
        and oldest_turn_started_at.tzinfo is not None
    ):
        stable_start = oldest_turn_started_at.astimezone(timezone.utc).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z")
        details.append(f"oldest active turn started at {stable_start}")
    else:
        details.append("start time unavailable")

    elapsed = occupancy.get("elapsed_seconds")
    if (
        isinstance(elapsed, (int, float))
        and not isinstance(elapsed, bool)
        and math.isfinite(elapsed)
        and elapsed >= 0
    ):
        details.append(f"elapsed {_format_age(int(elapsed))}")
    else:
        details.append("elapsed unavailable")

    return _line("Turn occupancy", "occupied", "; ".join(details))


def _dead_session_wakeup_line(
    data: dict,
    *,
    reference: datetime,
    stale_seconds: int,
) -> str:
    section = data.get("dead_session_wakeup")
    if not isinstance(section, dict):
        return _line("Dead-session wakeup", "unknown", "not reported")

    counter_names = ("scans", "scanned", "triggered", "delivered", "failed")
    enabled = section.get("enabled")
    if enabled is False:
        for name in counter_names:
            if name not in section:
                continue
            value = section[name]
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value != 0
            ):
                return _line(
                    "Dead-session wakeup",
                    "unknown",
                    "inconsistent disabled activity",
                )
        if section.get("last_scan_at") not in (None, ""):
            return _line(
                "Dead-session wakeup",
                "unknown",
                "inconsistent disabled activity",
            )
        return _line("Dead-session wakeup", "disabled")
    if enabled is not True:
        return _line("Dead-session wakeup", "unknown", "invalid enabled state")

    last_scan_at = _parse_iso(section.get("last_scan_at"))
    if last_scan_at is None or last_scan_at.tzinfo is None:
        return _line("Dead-session wakeup", "unknown", "scan time unavailable")
    scan_age = max(
        0,
        int(
            (
                reference - last_scan_at.astimezone(timezone.utc)
            ).total_seconds()
        ),
    )
    if scan_age > stale_seconds:
        return _line(
            "Dead-session wakeup",
            "unknown",
            f"scan stale: last observation {_format_age(scan_age)} ago",
        )

    counters: dict[str, int] = {}
    for name in counter_names:
        value = section.get(name)
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
        ):
            return _line("Dead-session wakeup", "unknown", "invalid counters")
        counters[name] = value
    if counters["scans"] == 0:
        return _line("Dead-session wakeup", "unknown", "inconsistent scan count")

    return _line(
        "Dead-session wakeup",
        "enabled",
        (
            f"scans={counters['scans']} scanned={counters['scanned']} "
            f"triggered={counters['triggered']} delivered={counters['delivered']} "
            f"failed={counters['failed']}; last scan {_format_age(scan_age)} ago"
        ),
    )


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
    reference = now or datetime.now(timezone.utc)

    if not health_path.exists():
        return [
            "🟡 Bot status: degraded",
            _line("Process", "alive", f"PID: {pid}"),
            _line("Service", "degraded", "health missing"),
            _line("Turn occupancy", "unknown", "health missing"),
            _line("Dead-session wakeup", "unknown", "health missing"),
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
            _line("Turn occupancy", "unknown", "health unreadable"),
            _line("Dead-session wakeup", "unknown", "health unreadable"),
            _line("Telegram", "degraded", "health unreadable"),
            _line(configured_label, "degraded", "health unreadable"),
        ]

    updated_at = _parse_iso(data.get("updated_at"))
    age_seconds = None
    if updated_at is not None:
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
            _line("Turn occupancy", "unknown", detail),
            _line("Dead-session wakeup", "unknown", detail),
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
        _turn_occupancy_line(
            data,
            reference=reference,
            stale_seconds=stale_seconds,
        ),
        _dead_session_wakeup_line(
            data,
            reference=reference,
            stale_seconds=stale_seconds,
        ),
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
