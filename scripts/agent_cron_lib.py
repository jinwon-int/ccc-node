#!/usr/bin/env python3
"""Pure schedule + retry helpers for the ccc-node agent-cron CLI.

Extracted from ``agent_cron.py`` so the deterministic cron-matching, schedule
parsing, and retry-backoff math can be imported and unit tested directly. This
module has no side effects: it only defines constants and pure functions over
their arguments. The import-safe ``agent_cron.py`` composition root imports these
names and dispatches only from ``main()``.
"""

import re
from datetime import datetime, timedelta, timezone

from zoneinfo import ZoneInfo

OCCURRENCE_SCAN_LIMIT = 1000
CRON_FIELD_RX = re.compile(r'^(\*|\*/[1-9][0-9]*|[0-9]+)(,(\*|\*/[1-9][0-9]*|[0-9]+))*$')
INTERVAL_RX = re.compile(r'^every\s+([1-9][0-9]*)\s*(m|h|d)$')
INTERVAL_UNIT_SECONDS = {'m': 60, 'h': 3600, 'd': 86400}
MAX_INTERVAL_SECONDS = 366 * 86400
SHORTHAND = {
    '@hourly': '0 * * * *',
    '@daily': '0 0 * * *',
    '@weekly': '0 0 * * 0',
    '@monthly': '0 0 1 * *',
    '@yearly': '0 0 1 1 *',
    '@annually': '0 0 1 1 *',
}


def resolve_timezone(name):
    """Resolve a task timezone name to a tzinfo, fail-closed on unknown names."""
    label = (name or 'UTC').strip() or 'UTC'
    if label.upper() == 'UTC':
        return timezone.utc, 'UTC'
    try:
        return ZoneInfo(label), label
    except Exception as e:
        raise ValueError(f'unknown timezone: {label}') from e


def parse_dt(value, field='timestamp'):
    if value is None or value == '':
        return None
    if not isinstance(value, str):
        raise ValueError(f'{field} must be an ISO8601 string')
    text = value.strip()
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as e:
        raise ValueError(f'{field} is not valid ISO8601: {value}') from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(second=0, microsecond=0)


def expand_field(raw, min_v, max_v):
    vals = set()
    for part in raw.split(','):
        if part == '*':
            vals.update(range(min_v, max_v + 1))
        elif part.startswith('*/'):
            step = int(part[2:])
            if step <= 0:
                raise ValueError('step must be positive')
            vals.update(range(min_v, max_v + 1, step))
        else:
            v = int(part)
            if v < min_v or v > max_v:
                raise ValueError(f'value {v} outside {min_v}-{max_v}')
            vals.add(v)
    return vals


def parse_local_dt(value, tz, field='schedule'):
    """Parse an ISO8601 timestamp; naive values are anchored to ``tz``."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{field} requires an ISO8601 timestamp')
    text = value.strip()
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as e:
        raise ValueError(f'{field} is not valid ISO8601: {value}') from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(timezone.utc).replace(second=0, microsecond=0)


def parse_schedule(expr, tz_name='UTC'):
    """Parse a schedule into a kind-aware spec.

    Supported forms:
    - 5-field cron / @shorthand  -> kind 'cron' (matched in the task timezone)
    - ``every <N>m|h|d``         -> kind 'interval' (fixed period)
    - ``at <ISO8601>`` or a bare ISO8601 timestamp -> kind 'once'
      (naive timestamps are anchored to the task timezone)
    """
    expr = (expr or '').strip()
    tz, tz_label = resolve_timezone(tz_name)
    if expr == '@reboot':
        raise ValueError('@reboot is not supported by dry-run due resolver')
    m = INTERVAL_RX.match(expr)
    if m:
        seconds = int(m.group(1)) * INTERVAL_UNIT_SECONDS[m.group(2)]
        if seconds < 60:
            raise ValueError('interval must be at least 1 minute')
        if seconds > MAX_INTERVAL_SECONDS:
            raise ValueError('interval must be at most 366 days')
        return {'kind': 'interval', 'seconds': seconds, 'expr': expr,
                'tz': tz, 'tzName': tz_label}
    if expr.startswith('at '):
        run_at = parse_local_dt(expr[3:], tz)
        return {'kind': 'once', 'runAt': run_at, 'expr': expr,
                'tz': tz, 'tzName': tz_label}
    if 'T' in expr and ' ' not in expr:
        run_at = parse_local_dt(expr, tz)
        return {'kind': 'once', 'runAt': run_at, 'expr': expr,
                'tz': tz, 'tzName': tz_label}
    expr = SHORTHAND.get(expr, expr)
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError(
            'schedule must be a supported @shorthand, 5-field cron, '
            '"every <N>m|h|d", or "at <ISO8601>"'
        )
    for p in parts:
        if not CRON_FIELD_RX.match(p):
            raise ValueError(f'unsupported cron field: {p}')
    return {
        'kind': 'cron',
        'minute': expand_field(parts[0], 0, 59),
        'hour': expand_field(parts[1], 0, 23),
        'dom': expand_field(parts[2], 1, 31),
        'month': expand_field(parts[3], 1, 12),
        'dow': expand_field(parts[4], 0, 7),
        'dom_any': parts[2] == '*',
        'dow_any': parts[4] == '*',
        'expr': expr,
        'tz': tz,
        'tzName': tz_label,
    }


def retry_policy(task):
    raw = task.get('retryPolicy') if isinstance(task, dict) else None
    if not isinstance(raw, dict):
        raw = {}
    max_attempts = raw.get('maxAttempts', 1)
    backoff_sec = raw.get('backoffSec', 60)
    multiplier = raw.get('backoffMultiplier', 2)
    max_backoff = raw.get('maxBackoffSec', 3600)
    def clamp_int(value, default, low, high):
        return value if isinstance(value, int) and low <= value <= high else default
    return {
        'maxAttempts': clamp_int(max_attempts, 1, 1, 10),
        'backoffSec': clamp_int(backoff_sec, 60, 0, 86400),
        'backoffMultiplier': clamp_int(multiplier, 2, 1, 10),
        'maxBackoffSec': clamp_int(max_backoff, 3600, 0, 86400),
    }


def retry_delay(policy, attempt):
    attempt_index = max(0, int(attempt or 1) - 1)
    delay = policy['backoffSec'] * (policy['backoffMultiplier'] ** attempt_index)
    return min(delay, policy['maxBackoffSec'])


def retry_view(task, at):
    state = task.get('retryState') if isinstance(task, dict) else None
    if not isinstance(state, dict):
        return None
    policy = retry_policy(task)
    attempt = state.get('attempt')
    if not isinstance(attempt, int) or attempt < 1:
        return None
    eligible_raw = state.get('retryEligibleAt')
    eligible = None
    if eligible_raw:
        try:
            eligible = parse_dt(eligible_raw, 'retryEligibleAt')
        except Exception:
            return {'state': state, 'policy': policy, 'valid': False, 'error': 'invalid retryEligibleAt'}
    ready = bool(eligible is not None and eligible <= at and attempt < policy['maxAttempts'])
    waiting = bool(eligible is not None and eligible > at and attempt < policy['maxAttempts'])
    exhausted = bool(attempt >= policy['maxAttempts'] or eligible is None)
    return {
        'state': state,
        'policy': policy,
        'valid': True,
        'retryEligibleAt': fmt_dt(eligible),
        'retryAttempt': attempt + 1 if ready else attempt,
        'ready': ready,
        'waiting': waiting,
        'exhausted': exhausted,
    }


def apply_retry_transition(task, scheduled_at, attempt, run_id, status, at):
    if status == 'success':
        existed = 'retryState' in task
        task.pop('retryState', None)
        return {'cleared': existed, 'attempt': attempt, 'retryEligibleAt': None, 'exhausted': False}
    policy = retry_policy(task)
    if attempt >= policy['maxAttempts']:
        task['retryState'] = {
            'scheduledAt': scheduled_at,
            'attempt': attempt,
            'retryEligibleAt': None,
            'lastStatus': 'exhausted',
            'lastRunId': run_id,
        }
        return {'cleared': False, 'attempt': attempt, 'retryEligibleAt': None, 'exhausted': True, 'policy': policy}
    delay = retry_delay(policy, attempt)
    eligible = at + timedelta(seconds=delay)
    eligible = eligible.replace(second=0, microsecond=0)
    task['retryState'] = {
        'scheduledAt': scheduled_at,
        'attempt': attempt,
        'retryEligibleAt': fmt_dt(eligible),
        'lastStatus': status,
        'lastRunId': run_id,
    }
    return {'cleared': False, 'attempt': attempt, 'retryEligibleAt': fmt_dt(eligible), 'exhausted': False, 'policy': policy}


def cron_matches(dt, spec):
    dow = (dt.weekday() + 1) % 7  # Python Mon=0; cron Sun=0
    dows = spec['dow']
    dom_match = dt.day in spec['dom']
    dow_match = dow in dows or (dow == 0 and 7 in dows)
    # Standard cron semantics: when both day-of-month and day-of-week are
    # restricted, either field may match. If one is '*', the restricted field
    # controls the day match.
    if not spec.get('dom_any') and not spec.get('dow_any'):
        day_match = dom_match or dow_match
    else:
        day_match = dom_match and dow_match
    return (
        dt.minute in spec['minute'] and
        dt.hour in spec['hour'] and
        day_match and
        dt.month in spec['month']
    )


def _local(dt, spec):
    tz = spec.get('tz') or timezone.utc
    return dt if tz is timezone.utc else dt.astimezone(tz)


def iter_occurrences(spec, start_exclusive, end_inclusive, cap=OCCURRENCE_SCAN_LIMIT):
    cur = (start_exclusive + timedelta(minutes=1)).replace(second=0, microsecond=0)
    end = end_inclusive.replace(second=0, microsecond=0)
    out = []
    truncated = False
    while cur <= end:
        if cron_matches(_local(cur, spec), spec):
            if len(out) >= cap:
                truncated = True
                break
            out.append(cur)
        cur += timedelta(minutes=1)
    return out, truncated


def next_occurrence(spec, after, max_minutes=366 * 24 * 60):
    cur = (after + timedelta(minutes=1)).replace(second=0, microsecond=0)
    for _ in range(max_minutes):
        if cron_matches(_local(cur, spec), spec):
            return cur
        cur += timedelta(minutes=1)
    return None


def _interval_next_after(spec, floor, anchor):
    """First phase-aligned occurrence strictly after ``floor`` (UTC)."""
    step = spec['seconds']
    if anchor is None:
        return floor + timedelta(seconds=step)
    delta = (floor - anchor).total_seconds()
    k = int(delta // step) + 1 if delta >= 0 else 0
    candidate = anchor + timedelta(seconds=k * step)
    while candidate <= floor:
        candidate += timedelta(seconds=step)
    return candidate


def schedule_occurrences(spec, last, at, anchor=None, cap=OCCURRENCE_SCAN_LIMIT):
    """Due occurrences (UTC, ascending) up to ``at``, plus a truncation flag.

    - once: due exactly when runAt <= at and it has not yet run at/after runAt.
    - interval: phase-anchored to ``anchor`` (or free-running from ``last``);
      a never-run task without an anchor is due once immediately.
    - cron: minute-scan matched in the task timezone (existing behavior).
    """
    kind = spec.get('kind', 'cron')
    if kind == 'once':
        run_at = spec['runAt']
        if run_at <= at and (last is None or last < run_at):
            return [run_at], False
        return [], False
    if kind == 'interval':
        if anchor is None and last is None:
            return [at], False
        floor = last if (anchor is None or (last is not None and last > anchor)) else anchor
        out = []
        truncated = False
        cur = _interval_next_after(spec, floor, anchor)
        while cur <= at:
            if len(out) >= cap:
                truncated = True
                break
            out.append(cur)
            cur += timedelta(seconds=spec['seconds'])
        return out, truncated
    horizon = last or (at - timedelta(days=366))
    return iter_occurrences(spec, horizon, at, cap)


def next_after(spec, at, anchor=None):
    """Next scheduled occurrence strictly after ``at`` (UTC), or None."""
    kind = spec.get('kind', 'cron')
    if kind == 'once':
        return spec['runAt'] if spec['runAt'] > at else None
    if kind == 'interval':
        return _interval_next_after(spec, at, anchor)
    return next_occurrence(spec, at)


def fmt_dt(dt):
    return None if dt is None else dt.isoformat().replace('+00:00', 'Z')
