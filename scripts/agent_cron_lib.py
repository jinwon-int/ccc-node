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

OCCURRENCE_SCAN_LIMIT = 1000
CRON_FIELD_RX = re.compile(r'^(\*|\*/[1-9][0-9]*|[0-9]+)(,(\*|\*/[1-9][0-9]*|[0-9]+))*$')
SHORTHAND = {
    '@hourly': '0 * * * *',
    '@daily': '0 0 * * *',
    '@weekly': '0 0 * * 0',
    '@monthly': '0 0 1 * *',
    '@yearly': '0 0 1 1 *',
    '@annually': '0 0 1 1 *',
}


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


def parse_schedule(expr):
    expr = expr.strip()
    if expr == '@reboot':
        raise ValueError('@reboot is not supported by dry-run due resolver')
    expr = SHORTHAND.get(expr, expr)
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError('schedule must be a supported @shorthand or 5-field cron')
    for p in parts:
        if not CRON_FIELD_RX.match(p):
            raise ValueError(f'unsupported cron field: {p}')
    return {
        'minute': expand_field(parts[0], 0, 59),
        'hour': expand_field(parts[1], 0, 23),
        'dom': expand_field(parts[2], 1, 31),
        'month': expand_field(parts[3], 1, 12),
        'dow': expand_field(parts[4], 0, 7),
        'dom_any': parts[2] == '*',
        'dow_any': parts[4] == '*',
        'expr': expr,
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


def iter_occurrences(spec, start_exclusive, end_inclusive, cap=OCCURRENCE_SCAN_LIMIT):
    cur = (start_exclusive + timedelta(minutes=1)).replace(second=0, microsecond=0)
    end = end_inclusive.replace(second=0, microsecond=0)
    out = []
    truncated = False
    while cur <= end:
        if cron_matches(cur, spec):
            if len(out) >= cap:
                truncated = True
                break
            out.append(cur)
        cur += timedelta(minutes=1)
    return out, truncated


def next_occurrence(spec, after, max_minutes=366 * 24 * 60):
    cur = (after + timedelta(minutes=1)).replace(second=0, microsecond=0)
    for _ in range(max_minutes):
        if cron_matches(cur, spec):
            return cur
        cur += timedelta(minutes=1)
    return None


def fmt_dt(dt):
    return None if dt is None else dt.isoformat().replace('+00:00', 'Z')
