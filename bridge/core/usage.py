"""Provider-neutral, bounded usage snapshots for the Telegram bridge."""

from __future__ import annotations

import json
import math
import os
import stat
import time
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping, Sequence

MAX_WINDOWS = 16
MAX_DAILY_BUCKETS = 14
MAX_SNAPSHOT_BYTES = 16 * 1024
SNAPSHOT_TTL_SECONDS = 15 * 60
MAX_TELEGRAM_USAGE_LENGTH = 3500
MAX_TOKEN_COUNT = 10**12


@dataclass(frozen=True, slots=True)
class UsageWindow:
    label: str
    used_percent: float
    duration_minutes: int | None = None
    resets_at: float | None = None


@dataclass(frozen=True, slots=True)
class DailyUsage:
    date: str
    tokens: int


@dataclass(frozen=True, slots=True)
class UsageSnapshot:
    provider: str
    plan_type: str | None = None
    windows: tuple[UsageWindow, ...] = ()
    context_used: int | None = None
    context_window: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    lifetime_tokens: int | None = None
    daily_usage: tuple[DailyUsage, ...] = ()
    total_cost_usd: float | None = None
    observed_at: float | None = None


def _number(value: object, *, maximum: float = 10**18) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0 or number > maximum:
        return None
    return number


def _integer(value: object, *, maximum: int = MAX_TOKEN_COUNT) -> int | None:
    number = _number(value, maximum=maximum)
    return int(number) if number is not None else None


def _percent(value: object) -> float | None:
    return _number(value, maximum=100)


def _text(value: object, *, maximum: int = 80) -> str | None:
    if not isinstance(value, str):
        return None
    clean = " ".join(value.split())[:maximum]
    return clean or None


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _window(label: str, value: object) -> UsageWindow | None:
    data = _mapping(value)
    used = _percent(data.get("usedPercent", data.get("used_percentage")))
    if used is None:
        return None
    return UsageWindow(
        label=_text(label) or "limit",
        used_percent=used,
        duration_minutes=_integer(
            data.get("windowDurationMins", data.get("window_minutes")), maximum=525_600
        ),
        resets_at=_number(data.get("resetsAt", data.get("resets_at")), maximum=10**11),
    )


def parse_codex_rate_limits(value: object) -> UsageSnapshot:
    """Parse the documented account/rateLimits/read response defensively."""

    root = _mapping(value)
    snapshots: list[tuple[str, Mapping[str, Any]]] = []
    current = _mapping(root.get("rateLimits"))
    by_id = _mapping(root.get("rateLimitsByLimitId"))
    for raw_id, raw in sorted(by_id.items(), key=lambda item: str(item[0]))[:MAX_WINDOWS]:
        limit_id = str(raw_id)
        if isinstance(raw, Mapping):
            snapshots.append((limit_id, raw))
    if not snapshots and current:
        snapshots.append(("default", current))

    plan_type: str | None = _text(current.get("planType"))
    windows: list[UsageWindow] = []
    seen: set[tuple[str, float, int | None, float | None]] = set()
    for fallback, snapshot in snapshots:
        plan_type = plan_type or _text(snapshot.get("planType"))
        limit_name = _text(snapshot.get("limitName")) or _text(snapshot.get("limitId")) or fallback
        for kind in ("primary", "secondary"):
            parsed = _window(f"{limit_name} {kind}", snapshot.get(kind))
            if parsed is None:
                continue
            signature = (
                parsed.label,
                parsed.used_percent,
                parsed.duration_minutes,
                parsed.resets_at,
            )
            if signature in seen:
                continue
            seen.add(signature)
            windows.append(parsed)
            if len(windows) >= MAX_WINDOWS:
                break
        if len(windows) >= MAX_WINDOWS:
            break
    return UsageSnapshot(
        provider="codex",
        plan_type=plan_type,
        windows=tuple(windows),
        observed_at=time.time(),
    )


def parse_codex_account_usage(value: object) -> UsageSnapshot:
    root = _mapping(value)
    summary = _mapping(root.get("summary"))
    buckets: list[DailyUsage] = []
    raw_buckets = root.get("dailyUsageBuckets")
    if isinstance(raw_buckets, Sequence) and not isinstance(raw_buckets, (str, bytes)):
        for raw in raw_buckets[:MAX_DAILY_BUCKETS]:
            data = _mapping(raw)
            date = _text(data.get("startDate"), maximum=32)
            tokens = _integer(data.get("tokens"))
            if date is not None and tokens is not None:
                buckets.append(DailyUsage(date, tokens))
    buckets.sort(key=lambda bucket: bucket.date, reverse=True)
    return UsageSnapshot(
        provider="codex",
        lifetime_tokens=_integer(summary.get("lifetimeTokens")),
        daily_usage=tuple(buckets[:MAX_DAILY_BUCKETS]),
    )


def parse_codex_thread_usage(value: object) -> UsageSnapshot:
    root = _mapping(value)
    token_usage = _mapping(root.get("tokenUsage", root))
    last = _mapping(token_usage.get("last"))
    total = _mapping(token_usage.get("total"))
    return UsageSnapshot(
        provider="codex",
        context_used=_integer(last.get("totalTokens")),
        context_window=_integer(token_usage.get("modelContextWindow")),
        input_tokens=_integer(total.get("inputTokens")),
        output_tokens=_integer(total.get("outputTokens")),
        total_tokens=_integer(total.get("totalTokens")),
        observed_at=time.time(),
    )


def parse_claude_rate_limit_event(message: object, *, observed_at: float | None = None) -> UsageSnapshot:
    """Parse a Claude Agent SDK ``RateLimitEvent`` from the live message stream.

    The CLI emits this natively (message type ``rate_limit_event``) whenever a
    rate-limit window's status transitions (``allowed`` -> ``allowed_warning``
    -> ``rejected``); no extra flag is required to receive it. This is the only
    source of Claude subscription rate-limit data in headless/SDK-driven
    sessions: the statusLine hook (``load_claude_status_snapshot``'s source)
    only fires from the interactive terminal status bar, which never renders
    here. Each event carries at most one window (``five_hour``/``seven_day``/
    etc.); callers accumulate across events via ``merge_usage``.
    """

    info = getattr(message, "rate_limit_info", None)
    label = _text(getattr(info, "rate_limit_type", None))
    utilization = _number(getattr(info, "utilization", None), maximum=1)
    windows: tuple[UsageWindow, ...] = ()
    if label and utilization is not None:
        windows = (
            UsageWindow(
                label=label.replace("_", " "),
                used_percent=min(100.0, utilization * 100),
                resets_at=_number(getattr(info, "resets_at", None), maximum=10**11),
            ),
        )
    return UsageSnapshot(
        provider="claude",
        windows=windows,
        observed_at=time.time() if observed_at is None else observed_at,
    )


def parse_claude_result(message: object, *, observed_at: float | None = None) -> UsageSnapshot:
    usage = _mapping(getattr(message, "usage", None))
    model_usage = _mapping(getattr(message, "model_usage", None))
    input_tokens = _integer(usage.get("input_tokens"))
    cache_creation = _integer(usage.get("cache_creation_input_tokens"))
    cache_read = _integer(usage.get("cache_read_input_tokens"))
    output_tokens = _integer(usage.get("output_tokens"))
    model_input = model_cache_creation = model_cache_read = model_output = 0
    saw_model_tokens = False
    for raw in list(model_usage.values())[:16]:
        model = _mapping(raw)
        values = (
            _integer(model.get("inputTokens", model.get("input_tokens"))) or 0,
            _integer(
                model.get("cacheCreationInputTokens", model.get("cache_creation_input_tokens"))
            )
            or 0,
            _integer(model.get("cacheReadInputTokens", model.get("cache_read_input_tokens"))) or 0,
            _integer(model.get("outputTokens", model.get("output_tokens"))) or 0,
        )
        if any(values):
            saw_model_tokens = True
        model_input += values[0]
        model_cache_creation += values[1]
        model_cache_read += values[2]
        model_output += values[3]
    if input_tokens is None and saw_model_tokens:
        input_tokens = model_input if model_input <= MAX_TOKEN_COUNT else None
    if cache_creation is None and saw_model_tokens:
        cache_creation = model_cache_creation if model_cache_creation <= MAX_TOKEN_COUNT else None
    if cache_read is None and saw_model_tokens:
        cache_read = model_cache_read if model_cache_read <= MAX_TOKEN_COUNT else None
    if output_tokens is None and saw_model_tokens:
        output_tokens = model_output if model_output <= MAX_TOKEN_COUNT else None
    context_used = None
    if input_tokens is not None:
        total_input = input_tokens + (cache_creation or 0) + (cache_read or 0)
        context_used = total_input if total_input <= MAX_TOKEN_COUNT else None

    # The SDK's model_usage values are version-dependent mappings. Only accept
    # the documented numeric context-window field when present; never render
    # arbitrary keys or model/account identifiers.
    context_window = None
    for raw in list(model_usage.values())[:16]:
        model = _mapping(raw)
        context_window = _integer(
            model.get("contextWindow", model.get("context_window")), maximum=10**9
        )
        if context_window is not None:
            break
    total_tokens = None
    if context_used is not None or output_tokens is not None:
        combined = (context_used or 0) + (output_tokens or 0)
        total_tokens = combined if combined <= MAX_TOKEN_COUNT else None
    return UsageSnapshot(
        provider="claude",
        context_used=context_used,
        context_window=context_window,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        total_cost_usd=_number(getattr(message, "total_cost_usd", None), maximum=10**9),
        observed_at=time.time() if observed_at is None else observed_at,
    )


def merge_usage(*snapshots: UsageSnapshot) -> UsageSnapshot:
    if not snapshots:
        raise ValueError("at least one usage snapshot is required")
    result = snapshots[0]
    for newer in snapshots[1:]:
        if newer.provider != result.provider:
            raise ValueError("cannot merge usage snapshots from different providers")
        windows_by_label = {window.label: window for window in result.windows}
        windows_by_label.update({window.label: window for window in newer.windows})
        result = UsageSnapshot(
            provider=result.provider,
            plan_type=newer.plan_type or result.plan_type,
            windows=tuple(windows_by_label[key] for key in sorted(windows_by_label))[:MAX_WINDOWS],
            context_used=(
                newer.context_used if newer.context_used is not None else result.context_used
            ),
            context_window=(
                newer.context_window if newer.context_window is not None else result.context_window
            ),
            input_tokens=(
                newer.input_tokens if newer.input_tokens is not None else result.input_tokens
            ),
            output_tokens=(
                newer.output_tokens if newer.output_tokens is not None else result.output_tokens
            ),
            total_tokens=(
                newer.total_tokens if newer.total_tokens is not None else result.total_tokens
            ),
            lifetime_tokens=(
                newer.lifetime_tokens
                if newer.lifetime_tokens is not None
                else result.lifetime_tokens
            ),
            daily_usage=newer.daily_usage or result.daily_usage,
            total_cost_usd=(
                newer.total_cost_usd if newer.total_cost_usd is not None else result.total_cost_usd
            ),
            observed_at=(
                newer.observed_at if newer.observed_at is not None else result.observed_at
            ),
        )
    return result


def status_snapshot_name(session_id: str) -> str:
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("session id must not be empty")
    return f"{sha256(session_id.encode('utf-8')).hexdigest()}.json"


def _open_directory_no_symlinks(path: Path) -> int:
    """Open an existing directory by dirfd-walking every path component."""

    absolute = Path(os.path.abspath(os.path.expanduser(str(path))))
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    current_fd = os.open(absolute.anchor or os.sep, flags)
    try:
        for component in absolute.parts[1:]:
            next_fd = os.open(component, flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def load_claude_status_snapshot(
    directory: Path,
    session_id: str,
    *,
    now: float | None = None,
    ttl_seconds: float = SNAPSHOT_TTL_SECONDS,
) -> UsageSnapshot | None:
    """Load one exact-session snapshot with owner/mode/symlink checks."""

    if ttl_seconds <= 0:
        return None
    try:
        name = status_snapshot_name(session_id)
        directory_fd = _open_directory_no_symlinks(directory)
    except (OSError, ValueError):
        return None
    try:
        directory_info = os.fstat(directory_fd)
        if (
            not stat.S_ISDIR(directory_info.st_mode)
            or directory_info.st_uid != os.getuid()
            or directory_info.st_mode & 0o077
        ):
            return None
        try:
            fd = os.open(
                name,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
        except OSError:
            return None
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_nlink != 1
                or info.st_mode & 0o077
                or info.st_size > MAX_SNAPSHOT_BYTES
            ):
                return None
            raw = os.read(fd, MAX_SNAPSHOT_BYTES + 1)
        finally:
            os.close(fd)
    finally:
        os.close(directory_fd)
    if len(raw) > MAX_SNAPSHOT_BYTES:
        return None
    try:
        data = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    root = _mapping(data)
    observed = _number(root.get("observedAt"), maximum=10**11)
    clock = time.time() if now is None else now
    if observed is None or observed > clock + 60 or clock - observed > ttl_seconds:
        return None

    context = _mapping(root.get("context"))
    raw_windows = root.get("rateLimits")
    windows: list[UsageWindow] = []
    if isinstance(raw_windows, Mapping):
        for key in sorted(str(item) for item in raw_windows)[:MAX_WINDOWS]:
            parsed = _window(key.replace("_", " "), raw_windows.get(key))
            if parsed is not None:
                windows.append(parsed)
    context_used = _integer(context.get("usedTokens"))
    output_tokens = _integer(context.get("outputTokens"))
    total_tokens = None
    if context_used is not None or output_tokens is not None:
        combined = (context_used or 0) + (output_tokens or 0)
        total_tokens = combined if combined <= MAX_TOKEN_COUNT else None
    return UsageSnapshot(
        provider="claude",
        windows=tuple(windows),
        context_used=context_used,
        context_window=_integer(context.get("contextWindow"), maximum=10**9),
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        total_cost_usd=_number(root.get("totalCostUsd"), maximum=10**9),
        observed_at=observed,
    )


def _format_reset(timestamp: float | None) -> str:
    if timestamp is None:
        return "reset unavailable"
    try:
        value = datetime.fromtimestamp(timestamp).astimezone()
    except (OverflowError, OSError, ValueError):
        return "reset unavailable"
    return value.strftime("%Y-%m-%d %H:%M %Z")


def _tokens(value: int | None) -> str:
    return f"{value:,}" if value is not None else "unavailable"


def render_usage(snapshot: UsageSnapshot) -> str:
    """Render a deterministic, bounded, Telegram-safe plain-text response."""

    title = "Codex" if snapshot.provider == "codex" else "Claude Code"
    lines = [f"📊 Usage · {title}", f"Plan: {snapshot.plan_type or 'unavailable'}"]
    if snapshot.windows:
        lines.append("Rate limits:")
        for window in snapshot.windows[:MAX_WINDOWS]:
            remaining = max(0.0, 100.0 - window.used_percent)
            duration = (
                f" · {window.duration_minutes}m window"
                if window.duration_minutes is not None
                else ""
            )
            lines.append(
                f"- {window.label}: {window.used_percent:g}% used / "
                f"{remaining:g}% left{duration} · {_format_reset(window.resets_at)}"
            )
    else:
        lines.append("Rate limits: unavailable")

    if snapshot.context_used is not None and snapshot.context_window:
        percent = min(100.0, snapshot.context_used * 100 / snapshot.context_window)
        lines.append(
            f"Context: {_tokens(snapshot.context_used)} / "
            f"{_tokens(snapshot.context_window)} ({percent:.1f}%)"
        )
    elif snapshot.context_used is not None:
        lines.append(f"Context: {_tokens(snapshot.context_used)} / unavailable")
    else:
        lines.append("Context: unavailable")
    lines.append(
        "Session tokens: "
        f"input {_tokens(snapshot.input_tokens)} · output {_tokens(snapshot.output_tokens)} "
        f"· total {_tokens(snapshot.total_tokens)}"
    )
    if snapshot.provider == "codex":
        lines.append(f"Account lifetime tokens: {_tokens(snapshot.lifetime_tokens)}")
        if snapshot.daily_usage:
            daily = ", ".join(
                f"{item.date} {_tokens(item.tokens)}" for item in snapshot.daily_usage[:7]
            )
            lines.append(f"Daily: {daily}")
        else:
            lines.append("Daily: unavailable")
    else:
        cost = (
            f"${snapshot.total_cost_usd:.4f}"
            if snapshot.total_cost_usd is not None
            else "unavailable"
        )
        lines.append(f"Session cost: {cost}")
    rendered = "\n".join(lines)
    if len(rendered) <= MAX_TELEGRAM_USAGE_LENGTH:
        return rendered
    return rendered[: MAX_TELEGRAM_USAGE_LENGTH - 2].rstrip() + "…"
