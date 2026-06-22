#!/usr/bin/env bash
# agent-cron — first-class durable task store/list/due surface.
#
# Issue #55 incremental slices:
# - store/list/validate is implemented.
# - due is a read-only dry-run resolver for schedule/catch-up planning.
# - lock implements local task-lock acquire/release/probe primitives.
# - run is an explicit manual execution path with lock acquire/release and
#   task-store lastRunAt/lastStatus/lastRunId updates. When notify is
#   telegram-owner it writes a redacted owner-only bridge spool entry, but never
#   calls Telegram/provider APIs directly.
# - run also appends a bounded runHistory entry for executed tasks.
# - run --dry-run remains a read-only execution-plan preview.
#
# This script intentionally does not install timers or touch live cron/systemd
# state.
set -uo pipefail

STORE="${CCC_AGENT_CRON_STORE:-$HOME/.claude/state/agent-cron/tasks.json}"
CMD="${1:-list}"
[ $# -gt 0 ] && shift
JSON=0
AT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --json) JSON=1 ;;
    --store) [ -n "${2:-}" ] || { echo "--store requires a path" >&2; exit 2; }; STORE="$2"; shift ;;
    --at) [ -n "${2:-}" ] || { echo "--at requires an ISO8601 timestamp" >&2; exit 2; }; AT="$2"; shift ;;
    -h|--help)
      cat <<'EOF'
Usage: agent-cron.sh [list|validate] [--store PATH] [--json]
       agent-cron.sh due [--store PATH] [--at ISO8601] [--json]
       agent-cron.sh lock <task-id> --action acquire|release|probe --run-id ID [--scheduled-at ISO8601] [--at ISO8601] [--json]
       agent-cron.sh run <task-id> --dry-run [--at ISO8601] [--json]

Implemented slices:
- list/validate: inspect and validate the task definition store.
- due: read-only dry-run schedule resolver. It reports due tasks, missed windows,
  catch-up policy, and lock paths, but never executes prompts or writes state.
- lock: local atomic task-lock acquire/release/probe primitives only. It writes
  lock files under the task store's sibling locks/ directory, but never executes
  prompts, sends notifications, installs schedulers, or updates task history.
- run --dry-run: read-only execution-plan preview. It combines due, lock probe,
  task policy, and headless command metadata, but never acquires locks, executes
  prompts, sends notifications, installs schedulers, or updates task history.
- run: explicit manual execution for due enabled tasks. It acquires the task lock,
  invokes ccc-headless, records lastRunAt/lastStatus/lastRunId, writes a
  redacted owner-only bridge spool entry when notify=telegram-owner, appends a
  bounded runHistory entry, and releases the lock in all normal failure/success
  paths. It still does not call Telegram
  or provider APIs, install schedulers, mutate crontab/systemd, or touch remotes.

No direct Telegram/API send, scheduler bootstrap, systemd/crontab writes,
provider sends, or remote-node actions are performed.
EOF
      exit 0
      ;;
    *) break ;;
  esac
  shift
done

case "$CMD" in
  list|validate|due|lock|run) ;;
  execute|scheduler|install|enable|disable|add|remove)
    echo "agent-cron $CMD is not implemented in this read-only slice; no filesystem changes were made." >&2
    exit 2
    ;;
  *) echo "Unknown command: $CMD" >&2; exit 2 ;;
esac

SCRIPT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export STORE JSON CMD AT SCRIPT_ROOT
export EXTRA_ARGS="$*"
python3 - <<'PY'
import json
import os
import re
import shlex
import socket
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

store = Path(os.environ['STORE']).expanduser()
json_out = os.environ.get('JSON') == '1'
cmd = os.environ['CMD']
at_raw = os.environ.get('AT') or ''

ALLOWED_NOTIFY = {'none', 'telegram-owner'}
ALLOWED_PERMISSION = {'dontAsk', 'acceptEdits', 'default', None}
ALLOWED_CATCH_UP = {'skip', 'once', 'all', None}
OCCURRENCE_SCAN_LIMIT = 1000
ID_RX = re.compile(r'^[A-Za-z0-9_.-]{1,96}$')
CRON_FIELD_RX = re.compile(r'^(\*|\*/[1-9][0-9]*|[0-9]+)(,(\*|\*/[1-9][0-9]*|[0-9]+))*$')
SHORTHAND = {
    '@hourly': '0 * * * *',
    '@daily': '0 0 * * *',
    '@weekly': '0 0 * * 0',
    '@monthly': '0 0 1 * *',
    '@yearly': '0 0 1 1 *',
    '@annually': '0 0 1 1 *',
}


def empty_doc():
    return {'version': 1, 'tasks': []}


def load_doc():
    if not store.exists():
        return empty_doc(), []
    try:
        data = json.loads(store.read_text(encoding='utf-8'))
    except Exception as e:
        return None, [f'invalid JSON store: {e}']
    return data, validate_doc(data)


def validate_doc(data):
    errors = []
    if not isinstance(data, dict):
        return ['store must be a JSON object']
    if data.get('version') != 1:
        errors.append('version must be 1')
    tasks = data.get('tasks')
    if not isinstance(tasks, list):
        errors.append('tasks must be an array')
        return errors
    seen = set()
    for idx, task in enumerate(tasks):
        prefix = f'tasks[{idx}]'
        if not isinstance(task, dict):
            errors.append(f'{prefix} must be an object')
            continue
        tid = task.get('id')
        if not isinstance(tid, str) or not ID_RX.match(tid):
            errors.append(f'{prefix}.id must match {ID_RX.pattern}')
        elif tid in seen:
            errors.append(f'duplicate task id: {tid}')
        else:
            seen.add(tid)
        schedule = task.get('schedule')
        if not isinstance(schedule, str) or not schedule.strip():
            errors.append(f'{prefix}.schedule is required')
        prompt = task.get('prompt')
        if not isinstance(prompt, str) or not prompt.strip():
            errors.append(f'{prefix}.prompt is required')
        if not isinstance(task.get('enabled'), bool):
            errors.append(f'{prefix}.enabled must be boolean')
        notify = task.get('notify', 'none')
        if notify not in ALLOWED_NOTIFY:
            errors.append(f'{prefix}.notify must be one of {sorted(ALLOWED_NOTIFY)}')
        perm = task.get('permissionMode')
        if perm not in ALLOWED_PERMISSION:
            errors.append(f'{prefix}.permissionMode unsupported: {perm}')
        allowed = task.get('allowedTools', [])
        if not isinstance(allowed, list) or any(not isinstance(x, str) for x in allowed):
            errors.append(f'{prefix}.allowedTools must be an array of strings')
        for field in ('attachMemory', 'attachSkills'):
            val = task.get(field, [])
            if not isinstance(val, list) or any(not isinstance(x, str) for x in val):
                errors.append(f'{prefix}.{field} must be an array of strings')
        catch_up = task.get('catchUpPolicy')
        if catch_up not in ALLOWED_CATCH_UP:
            errors.append(f'{prefix}.catchUpPolicy must be one of skip, once, all')
        max_catch_up = task.get('maxCatchup', 1)
        if not isinstance(max_catch_up, int) or max_catch_up < 1 or max_catch_up > 100:
            errors.append(f'{prefix}.maxCatchup must be an integer from 1 to 100')
        lock_timeout = task.get('lockTimeoutSec', 0)
        if not isinstance(lock_timeout, int) or lock_timeout < 0 or lock_timeout > 86400:
            errors.append(f'{prefix}.lockTimeoutSec must be an integer from 0 to 86400')
        max_run_history = task.get('maxRunHistory', 20)
        if not isinstance(max_run_history, int) or max_run_history < 1 or max_run_history > 500:
            errors.append(f'{prefix}.maxRunHistory must be an integer from 1 to 500')
        run_history = task.get('runHistory', [])
        if not isinstance(run_history, list):
            errors.append(f'{prefix}.runHistory must be an array')
        else:
            for hidx, item in enumerate(run_history):
                hp = f'{prefix}.runHistory[{hidx}]'
                if not isinstance(item, dict):
                    errors.append(f'{hp} must be an object')
                    continue
                for field in ('runId', 'scheduledAt', 'startedAt', 'status'):
                    if not isinstance(item.get(field), str) or not item.get(field):
                        errors.append(f'{hp}.{field} must be a non-empty string')
                if 'finishedAt' in item and item['finishedAt'] is not None and not isinstance(item['finishedAt'], str):
                    errors.append(f'{hp}.finishedAt must be string or null')
                if 'exitCode' in item and item['exitCode'] is not None and not isinstance(item['exitCode'], int):
                    errors.append(f'{hp}.exitCode must be integer or null')
                if not isinstance(item.get('attempt', 1), int) or item.get('attempt', 1) < 1:
                    errors.append(f'{hp}.attempt must be a positive integer')
                if not isinstance(item.get('notifyState', 'none'), str):
                    errors.append(f'{hp}.notifyState must be a string')
        timezone_name = task.get('timezone', 'UTC')
        if timezone_name != 'UTC':
            errors.append(f'{prefix}.timezone currently supports UTC only')
        for field in ('lastRunAt', 'lastStatus', 'lastRunId'):
            if field in task and task[field] is not None and not isinstance(task[field], str):
                errors.append(f'{prefix}.{field} must be string or null')
        if 'redactProfile' in task and task['redactProfile'] is not None and not isinstance(task['redactProfile'], str):
            errors.append(f'{prefix}.redactProfile must be string or null')
    return errors


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


def lock_path(task_id):
    base = store.parent if str(store.parent) != '.' else Path.cwd()
    return base / 'locks' / f'{task_id}.lock'


def boot_id():
    try:
        return Path('/proc/sys/kernel/random/boot_id').read_text(encoding='utf-8').strip()
    except OSError:
        return ''


def task_by_id(data, task_id):
    for task in data.get('tasks', []):
        if task.get('id') == task_id:
            return task
    return None


def read_lock(task_id):
    path = lock_path(task_id)
    if not path.exists():
        return None, None
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, dict):
            data = {'raw': data}
    except Exception as e:
        data = {'error': f'invalid lock JSON: {e}'}
    return path, data


def lock_age(lock, at):
    try:
        acquired = parse_dt(lock.get('acquiredAt'), 'acquiredAt')
    except Exception:
        return None
    if acquired is None:
        return None
    return max(0, int((at - acquired).total_seconds()))


def lock_status(task_id, task, at):
    path, lock = read_lock(task_id)
    base = {'lockPath': str(lock_path(task_id)), 'lockState': 'free'}
    if lock is None:
        return base
    timeout = task.get('lockTimeoutSec', 0) if isinstance(task, dict) else 0
    age = lock_age(lock, at)
    current_boot = boot_id()
    stale = bool(lock.get('error'))
    if not stale and lock.get('bootId') and current_boot and lock.get('bootId') != current_boot:
        stale = True
    if not stale and timeout and age is not None and age > timeout:
        stale = True
    state = 'stale' if stale else 'held'
    base.update({'lockState': state, 'holder': lock, 'lockAgeSec': age, 'lockTimeoutSec': timeout})
    return base


def write_lock(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(str(path), flags, 0o600)
    try:
        os.write(fd, (json.dumps(payload, ensure_ascii=False, sort_keys=True) + '\n').encode('utf-8'))
    finally:
        os.close(fd)


def parse_lock_args():
    args = shlex.split(os.environ.get('EXTRA_ARGS', ''))
    if not args:
        raise ValueError('lock requires a task id')
    task_id = args[0]
    action = 'probe'
    run_id = ''
    scheduled_at = ''
    at_value = at_raw
    local_json = json_out
    i = 1
    while i < len(args):
        a = args[i]
        if a == '--json':
            local_json = True
        elif a == '--action':
            i += 1
            if i >= len(args):
                raise ValueError('--action requires a value')
            action = args[i]
        elif a == '--run-id':
            i += 1
            if i >= len(args):
                raise ValueError('--run-id requires a value')
            run_id = args[i]
        elif a == '--scheduled-at':
            i += 1
            if i >= len(args):
                raise ValueError('--scheduled-at requires a value')
            scheduled_at = args[i]
        elif a == '--at':
            i += 1
            if i >= len(args):
                raise ValueError('--at requires a value')
            at_value = args[i]
        else:
            raise ValueError(f'unsupported lock argument: {a}')
        i += 1
    if action not in {'probe', 'acquire', 'release'}:
        raise ValueError('--action must be one of acquire, release, probe')
    if action in {'acquire', 'release'} and not run_id:
        raise ValueError('--run-id is required for acquire/release')
    return task_id, action, run_id, scheduled_at, at_value, local_json


def emit_lock(result, as_json):
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"agent-cron lock {result.get('taskId')}: {result.get('lockState')} ok={str(result.get('ok')).lower()}")


def lock_command(data):
    try:
        task_id, action, run_id, scheduled_at, at_value, as_json = parse_lock_args()
        at = parse_dt(at_value, '--at') if at_value else datetime.now(timezone.utc).replace(second=0, microsecond=0)
        task = task_by_id(data, task_id)
        if not task:
            return {'ok': False, 'taskId': task_id, 'lockState': 'unknown-task', 'error': 'task id not found'}, as_json, 1
        path = lock_path(task_id)
        status = lock_status(task_id, task, at)
        result = {'ok': True, 'taskId': task_id, 'action': action, **status}
        if action == 'probe':
            return result, as_json, 0
        if action == 'release':
            holder = status.get('holder') or {}
            if status['lockState'] == 'free':
                result.update({'lockState': 'free', 'ok': True})
                return result, as_json, 0
            if holder.get('runId') != run_id:
                result.update({'ok': False, 'lockState': 'release-mismatch', 'runId': run_id})
                return result, as_json, 1
            path.unlink()
            result.update({'ok': True, 'lockState': 'released', 'runId': run_id})
            return result, as_json, 0
        # acquire
        reclaimed = False
        if status['lockState'] == 'held':
            result.update({'ok': False, 'lockState': 'held', 'runId': run_id})
            return result, as_json, 1
        if status['lockState'] == 'stale':
            stale_path = path.with_name(f'{path.name}.stale.{int(at.timestamp())}.{os.getpid()}')
            path.rename(stale_path)
            reclaimed = True
        payload = {
            'taskId': task_id,
            'runId': run_id,
            'pid': os.getpid(),
            'host': socket.gethostname(),
            'bootId': boot_id(),
            'acquiredAt': fmt_dt(at),
            'scheduledAt': scheduled_at or fmt_dt(at),
        }
        try:
            write_lock(path, payload)
        except FileExistsError:
            after = lock_status(task_id, task, at)
            return {'ok': False, 'taskId': task_id, 'action': action, **after, 'runId': run_id}, as_json, 1
        result = {'ok': True, 'taskId': task_id, 'action': action, 'lockPath': str(path), 'lockState': 'acquired', 'runId': run_id, 'reclaimedStale': reclaimed, 'holder': payload}
        return result, as_json, 0
    except Exception as e:
        return {'ok': False, 'taskId': None, 'lockState': 'error', 'error': str(e)}, json_out, 1


def normalize(data):
    out = {'version': 1, 'tasks': []}
    for t in data.get('tasks', []):
        out['tasks'].append({
            'id': t.get('id'),
            'name': t.get('name') or t.get('id'),
            'schedule': t.get('schedule'),
            'enabled': bool(t.get('enabled')),
            'notify': t.get('notify', 'none'),
            'allowedTools': t.get('allowedTools', []),
            'permissionMode': t.get('permissionMode') or 'default',
            'attachMemory': t.get('attachMemory', []),
            'attachSkills': t.get('attachSkills', []),
            'timezone': t.get('timezone', 'UTC'),
            'catchUpPolicy': t.get('catchUpPolicy', 'skip'),
            'maxCatchup': t.get('maxCatchup', 1),
            'lockTimeoutSec': t.get('lockTimeoutSec', 0),
            'maxRunHistory': t.get('maxRunHistory', 20),
            'redactProfile': t.get('redactProfile', 'default'),
            'lastRunAt': t.get('lastRunAt'),
            'lastStatus': t.get('lastStatus'),
            'lastRunId': t.get('lastRunId'),
            'runHistoryCount': len(t.get('runHistory', [])) if isinstance(t.get('runHistory', []), list) else 0,
        })
    out['tasks'].sort(key=lambda x: x['id'] or '')
    return out


def due_plan(data):
    try:
        at = parse_dt(at_raw, '--at') if at_raw else datetime.now(timezone.utc).replace(second=0, microsecond=0)
    except Exception as e:
        return {
            'ok': False,
            'store': str(store),
            'at': at_raw,
            'mode': 'dry-run-read-only',
            'tasks': [],
            'errors': [str(e)],
        }
    rows = []
    errors = []
    for idx, task in enumerate(data.get('tasks', [])):
        tid = task.get('id')
        lock = lock_status(tid or f'task-{idx}', task, at)
        row = {
            'id': tid,
            'enabled': bool(task.get('enabled')),
            'schedule': task.get('schedule'),
            'timezone': task.get('timezone', 'UTC'),
            'catchUpPolicy': task.get('catchUpPolicy', 'skip'),
            'maxCatchup': task.get('maxCatchup', 1),
            'lastRunAt': task.get('lastRunAt'),
            'due': False,
            'dueCount': 0,
            'missedRuns': 0,
            'missedRunsTruncated': False,
            'occurrenceScanLimit': OCCURRENCE_SCAN_LIMIT,
            'scheduledAt': None,
            'nextDueAt': None,
            'lockPath': lock['lockPath'],
            'lockState': lock['lockState'],
            'status': 'disabled' if not task.get('enabled') else 'idle',
        }
        for k in ('holder', 'lockAgeSec', 'lockTimeoutSec'):
            if k in lock:
                row[k] = lock[k]
        try:
            spec = parse_schedule(task.get('schedule') or '')
            last = parse_dt(task.get('lastRunAt'), f'tasks[{idx}].lastRunAt')
            horizon_start = last or (at - timedelta(days=366))
            occurrences, truncated = iter_occurrences(spec, horizon_start, at)
            raw_missed = len(occurrences)
            row['missedRunsTruncated'] = truncated
            policy = task.get('catchUpPolicy', 'skip')
            max_catch = task.get('maxCatchup', 1)
            if task.get('enabled') and raw_missed > 0:
                row['due'] = True
                row['scheduledAt'] = fmt_dt(occurrences[-1])
                if policy == 'all':
                    row['dueCount'] = min(raw_missed, max_catch)
                else:
                    row['dueCount'] = 1
                row['missedRuns'] = max(0, raw_missed - row['dueCount'])
                if row['lockState'] == 'held':
                    row['status'] = 'locked'
                elif row['lockState'] == 'stale':
                    row['status'] = 'stale-lock'
                else:
                    row['status'] = 'due'
            row['nextDueAt'] = fmt_dt(next_occurrence(spec, at))
        except Exception as e:
            row['status'] = 'invalid-schedule'
            row['error'] = str(e)
            errors.append(f'{tid}: {e}')
        rows.append(row)
    return {'ok': not errors, 'store': str(store), 'at': fmt_dt(at), 'mode': 'dry-run-read-only', 'tasks': rows, 'errors': errors}


def parse_run_args():
    args = shlex.split(os.environ.get('EXTRA_ARGS', ''))
    if not args:
        raise ValueError('run requires a task id')
    task_id = args[0]
    dry_run = False
    at_value = at_raw
    local_json = json_out
    i = 1
    while i < len(args):
        a = args[i]
        if a == '--dry-run':
            dry_run = True
        elif a == '--json':
            local_json = True
        elif a == '--at':
            i += 1
            if i >= len(args):
                raise ValueError('--at requires a value')
            at_value = args[i]
        else:
            raise ValueError(f'unsupported run argument: {a}')
        i += 1
    return task_id, dry_run, at_value, local_json


def run_plan_for(data, task_id, at_value):
    global at_raw
    old_at = at_raw
    at_raw = at_value or at_raw
    try:
        plan = due_plan(data)
    finally:
        at_raw = old_at
    row = next((t for t in plan.get('tasks', []) if t.get('id') == task_id), None)
    return plan, row


def run_dry_plan(data):
    try:
        task_id, dry_run, at_value, as_json = parse_run_args()
        if not dry_run:
            return run_execute(data, task_id, at_value, as_json)
        task = task_by_id(data, task_id)
        if not task:
            return {'ok': False, 'mode': 'run-dry-run-read-only', 'taskId': task_id, 'error': 'task id not found'}, as_json, 1
        plan, row = run_plan_for(data, task_id, at_value)
        if row is None:
            return {'ok': False, 'mode': 'run-dry-run-read-only', 'taskId': task_id, 'error': 'task id not found in due plan'}, as_json, 1
        headless_cmd = os.environ.get('CCC_HEADLESS_CMD') or str(Path(os.environ.get('SCRIPT_ROOT', '.')) / 'claude' / 'headless.sh')
        notify = task.get('notify', 'none')
        result = {
            'ok': plan.get('ok', False),
            'mode': 'run-dry-run-read-only',
            'store': str(store),
            'at': plan.get('at'),
            'taskId': task_id,
            'due': bool(row.get('due')),
            'status': row.get('status'),
            'scheduledAt': row.get('scheduledAt'),
            'dueCount': row.get('dueCount', 0),
            'missedRuns': row.get('missedRuns', 0),
            'lock': {
                'state': row.get('lockState'),
                'path': row.get('lockPath'),
                'holder': row.get('holder'),
                'probeOnly': True,
            },
            'headless': headless_metadata(task, execute=False),
            'notification': {
                'policy': notify,
                'delivery': 'preview-only' if notify == 'telegram-owner' else 'none',
                'redactProfile': task.get('redactProfile', 'default'),
                'send': False,
            },
            'mutations': mutation_flags(False, False, False),
            'errors': plan.get('errors', []),
        }
        return result, as_json, 0 if result['ok'] else 1
    except Exception as e:
        return {'ok': False, 'mode': 'run-dry-run-read-only', 'taskId': None, 'error': str(e)}, json_out, 1


def headless_metadata(task, execute):
    headless_cmd = os.environ.get('CCC_HEADLESS_CMD') or str(Path(os.environ.get('SCRIPT_ROOT', '.')) / 'claude' / 'headless.sh')
    return {
        'command': headless_cmd,
        'promptBytes': len((task.get('prompt') or '').encode('utf-8')),
        'permissionMode': task.get('permissionMode') or 'default',
        'allowedTools': task.get('allowedTools', []),
        'attachMemory': task.get('attachMemory', []),
        'attachSkills': task.get('attachSkills', []),
        'execute': bool(execute),
    }


def mutation_flags(lock_acquire, task_write, headless_execute, push_spool_write=False, history_append=False):
    return {
        'lockAcquire': bool(lock_acquire),
        'taskStoreWrite': bool(task_write),
        'historyAppend': bool(history_append),
        'pushSpoolWrite': bool(push_spool_write),
        'schedulerInstall': False,
        'headlessExecute': bool(headless_execute),
    }


def write_doc(data):
    store.parent.mkdir(parents=True, exist_ok=True)
    tmp = store.with_name(f'.{store.name}.tmp.{os.getpid()}')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False) + '\n', encoding='utf-8')
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(store)


def history_attempt(task, scheduled_at):
    attempts = 0
    history = task.get('runHistory', [])
    if isinstance(history, list):
        for item in history:
            if isinstance(item, dict) and item.get('scheduledAt') == scheduled_at:
                attempts = max(attempts, int(item.get('attempt') or 0))
    return attempts + 1


def append_run_history(task, entry):
    history = task.get('runHistory')
    if not isinstance(history, list):
        history = []
    history.append(entry)
    max_history = task.get('maxRunHistory', 20)
    if not isinstance(max_history, int) or max_history < 1:
        max_history = 20
    if max_history > 500:
        max_history = 500
    task['runHistory'] = history[-max_history:]


def acquire_for_run(task_id, task, run_id, scheduled_at, at):
    path = lock_path(task_id)
    status = lock_status(task_id, task, at)
    if status['lockState'] == 'held':
        return False, {'state': 'held', 'path': str(path), 'holder': status.get('holder')}
    if status['lockState'] == 'stale':
        stale_path = path.with_name(f'{path.name}.stale.{int(at.timestamp())}.{os.getpid()}')
        try:
            path.rename(stale_path)
        except FileNotFoundError:
            after = lock_status(task_id, task, at)
            return False, {'state': after.get('lockState', 'held'), 'path': str(path), 'holder': after.get('holder')}
        except OSError as e:
            return False, {'state': 'error', 'path': str(path), 'error': str(e)}
    payload = {
        'taskId': task_id,
        'runId': run_id,
        'pid': os.getpid(),
        'host': socket.gethostname(),
        'bootId': boot_id(),
        'acquiredAt': fmt_dt(at),
        'scheduledAt': scheduled_at or fmt_dt(at),
    }
    try:
        write_lock(path, payload)
    except FileExistsError:
        after = lock_status(task_id, task, at)
        return False, {'state': after.get('lockState', 'held'), 'path': str(path), 'holder': after.get('holder')}
    return True, {'state': 'acquired', 'path': str(path), 'holder': payload}


def release_for_run(task_id, run_id):
    path, lock = read_lock(task_id)
    if lock is None:
        return {'ok': True, 'state': 'free'}
    if lock.get('runId') != run_id:
        return {'ok': False, 'state': 'release-mismatch', 'holder': lock}
    path.unlink()
    return {'ok': True, 'state': 'released'}


def short_text(text, limit=4000):
    if text is None:
        return ''
    text = str(text)
    return text if len(text) <= limit else text[:limit] + f'\n[truncated {len(text)-limit} chars]'


def run_headless(task):
    meta = headless_metadata(task, execute=True)
    cmd = shlex.split(meta['command'])
    if not cmd:
        raise ValueError('CCC_HEADLESS_CMD resolved to an empty command')
    env = os.environ.copy()
    allowed = task.get('allowedTools', [])
    if allowed:
        env['CCC_ALLOWED_TOOLS'] = ','.join(allowed)
    perm = task.get('permissionMode') or 'default'
    if perm != 'default':
        env['CCC_PERMISSION_MODE'] = perm
    prompt = task.get('prompt') or ''
    proc = subprocess.run(cmd + [prompt], text=True, input='', capture_output=True, env=env)
    return {
        **meta,
        'exitCode': proc.returncode,
        'stdout': short_text(proc.stdout),
        'stderr': short_text(proc.stderr),
    }


def push_spool_dir():
    raw = (
        os.environ.get('CCC_AGENT_CRON_PUSH_SPOOL') or
        os.environ.get('CCC_PUSH_SPOOL') or
        str(Path.home() / '.claude' / 'state' / 'telegram-spool')
    )
    return Path(raw).expanduser()


def safe_name(value):
    return re.sub(r'[^A-Za-z0-9_.-]+', '-', str(value or 'unknown'))[:120] or 'unknown'


def redact_for_owner(text, limit=1200):
    text = short_text(text or '', limit)
    # Mask common secret-bearing assignments and bearer headers first, then long
    # token-shaped runs. Keep this conservative: the spool is operator-visible.
    patterns = [
        (r'(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}', r'\1[REDACTED]'),
        (r'(?i)\b(token|secret|password|passwd|api[_-]?key|authorization)\s*[:=]\s*[^\s,;]+', r'\1=[REDACTED]'),
        (r'\b(ghp|gho|ghu|ghs|github_pat|sk|xox[baprs])-?[A-Za-z0-9_./+=-]{8,}', r'[REDACTED]'),
        (r'[A-Za-z0-9_./+=-]{24,}', r'[REDACTED]'),
    ]
    for rx, repl in patterns:
        text = re.sub(rx, repl, text)
    return text


def notification_base(task):
    notify = task.get('notify', 'none')
    return {
        'policy': notify,
        'send': False,
        'delivery': 'none' if notify == 'none' else 'not-attempted',
        'redactProfile': task.get('redactProfile', 'default'),
    }


def build_owner_text(task_id, run_id, scheduled_at, status, headless):
    stdout = redact_for_owner((headless or {}).get('stdout', ''), 900).strip()
    stderr = redact_for_owner((headless or {}).get('stderr', ''), 900).strip()
    lines = [
        f"agent-cron task {task_id} finished with status={status}",
        f"scheduledAt={scheduled_at or ''}",
        f"runId={run_id}",
        f"exitCode={(headless or {}).get('exitCode', '')}",
    ]
    if stdout:
        lines.append('stdout: ' + stdout.replace('\n', ' ')[:900])
    if stderr:
        lines.append('stderr: ' + stderr.replace('\n', ' ')[:900])
    return '\n'.join(lines)


def write_owner_spool(task, task_id, run_id, scheduled_at, status, headless, at):
    base = notification_base(task)
    if task.get('notify', 'none') != 'telegram-owner':
        return base
    spool = push_spool_dir()
    text = build_owner_text(task_id, run_id, scheduled_at, status, headless)
    ts = fmt_dt(at) or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    payload = {
        'version': 1,
        'ts': ts,
        'event': 'AgentCronRun',
        'node': socket.gethostname(),
        'text': text,
        'dedup': f'agent-cron:{task_id}:{run_id}:{status}',
        'recipient': 'owner',
        'taskId': task_id,
        'runId': run_id,
        'scheduledAt': scheduled_at,
        'status': status,
        'redactProfile': task.get('redactProfile', 'default'),
        'redacted': True,
        'send': False,
        'delivery': 'spooled',
    }
    try:
        spool.mkdir(parents=True, exist_ok=True)
        name = f"{safe_name(ts)}-{safe_name(task_id)}-{safe_name(run_id)}.json"
        path = spool / name
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(str(path), flags, 0o600)
        try:
            os.write(fd, (json.dumps(payload, ensure_ascii=False, sort_keys=True) + '\n').encode('utf-8'))
        finally:
            os.close(fd)
        payload['path'] = str(path)
        return {**base, 'delivery': 'spooled', 'redacted': True, 'spoolPath': str(path)}
    except Exception as e:
        return {**base, 'delivery': 'spool-error', 'redacted': True, 'error': short_text(str(e), 600)}


def run_execute(data, task_id, at_value, as_json):
    task = task_by_id(data, task_id)
    if not task:
        return {'ok': False, 'mode': 'run-execute', 'taskId': task_id, 'error': 'task id not found'}, as_json, 1
    plan, row = run_plan_for(data, task_id, at_value)
    if row is None:
        return {'ok': False, 'mode': 'run-execute', 'taskId': task_id, 'error': 'task id not found in due plan'}, as_json, 1
    base = {
        'mode': 'run-execute',
        'store': str(store),
        'at': plan.get('at'),
        'taskId': task_id,
        'scheduledAt': row.get('scheduledAt'),
        'due': bool(row.get('due')),
        'notification': notification_base(task),
    }
    if not task.get('enabled'):
        return {**base, 'ok': True, 'status': 'disabled', 'mutations': mutation_flags(False, False, False)}, as_json, 0
    if not row.get('due'):
        return {**base, 'ok': True, 'status': 'not-due', 'mutations': mutation_flags(False, False, False)}, as_json, 0
    at = parse_dt(plan.get('at'), '--at')
    scheduled_at = row.get('scheduledAt') or fmt_dt(at)
    run_id = f'{task_id}-{int(at.timestamp())}-{os.getpid()}'
    acquired, lock = acquire_for_run(task_id, task, run_id, scheduled_at, at)
    if not acquired:
        return {**base, 'ok': False, 'status': 'locked' if lock.get('state') in {'held','stale'} else lock.get('state','lock-error'), 'runId': run_id, 'lock': lock, 'mutations': mutation_flags(False, False, False)}, as_json, 1
    headless = None
    release = {'ok': False, 'state': 'not-attempted'}
    notification = notification_base(task)
    status = 'failed'
    ok = False
    rc = 1
    try:
        try:
            headless = run_headless(task)
            ok = headless.get('exitCode') == 0
            status = 'success' if ok else 'failed'
            rc = 0 if ok else 1
        except Exception as e:
            headless = {
                **headless_metadata(task, execute=True),
                'exitCode': 127,
                'stdout': '',
                'stderr': short_text(str(e)),
            }
            ok = False
            status = 'failed'
            rc = 1
        notification = write_owner_spool(task, task_id, run_id, scheduled_at, status, headless, at)
        notify_state = notification.get('delivery') or 'none'
        if task.get('notify', 'none') == 'none':
            notify_state = 'none'
        entry = {
            'runId': run_id,
            'scheduledAt': scheduled_at,
            'startedAt': fmt_dt(at),
            'finishedAt': fmt_dt(at),
            'status': status,
            'exitCode': headless.get('exitCode') if isinstance(headless, dict) else None,
            'attempt': history_attempt(task, scheduled_at),
            'notifyState': notify_state,
        }
        append_run_history(task, entry)
        task['lastRunAt'] = scheduled_at
        task['lastStatus'] = status
        task['lastRunId'] = run_id
        write_doc(data)
    finally:
        release = release_for_run(task_id, run_id)
    result = {
        **base,
        'ok': ok,
        'status': status,
        'runId': run_id,
        'lock': {'state': lock.get('state'), 'path': lock.get('path'), 'release': release},
        'headless': headless or headless_metadata(task, execute=True),
        'notification': notification,
        'mutations': mutation_flags(True, headless is not None, headless is not None, notification.get('delivery') == 'spooled', True),
    }
    if not release.get('ok'):
        result['ok'] = False
        result['releaseError'] = release
        rc = 1
    return result, as_json, rc


def emit_run(result, as_json):
    if result is None:
        print('agent-cron run returned no result', file=sys.stderr)
        return
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        title = '# agent-cron run dry-run plan' if result.get('mode') == 'run-dry-run-read-only' else '# agent-cron run result'
        print(title + '\n')
        print(f"- task: `{result['taskId']}`")
        print(f"- at: `{result['at']}`")
        print(f"- due: `{str(result.get('due')).lower()}` status=`{result.get('status')}` scheduledAt=`{result.get('scheduledAt') or ''}`")
        if result.get('lock'):
            print(f"- lock: `{result['lock'].get('state')}` `{result['lock'].get('path')}`")
        if result.get('headless'):
            print(f"- headless: `{result['headless'].get('command')}` execute=`{str(result['headless'].get('execute')).lower()}` exit=`{result['headless'].get('exitCode', '')}`")
        print(f"- mutations: `{json.dumps(result.get('mutations', {}), ensure_ascii=False, sort_keys=True)}`")


data, errors = load_doc()
if errors:
    for e in errors:
        print(f'agent-cron: {e}', file=sys.stderr)
    sys.exit(1)

if cmd == 'lock':
    result, as_json, rc = lock_command(data)
    emit_lock(result, as_json)
    sys.exit(rc)

if cmd == 'run':
    result, as_json, rc = run_dry_plan(data)
    emit_run(result, as_json)
    sys.exit(rc)

if cmd == 'validate':
    if json_out:
        print(json.dumps({'ok': True, 'store': str(store), 'tasks': len(data.get('tasks', []))}, ensure_ascii=False, indent=2))
    else:
        print(f'agent-cron store OK: {store} ({len(data.get("tasks", []))} task(s))')
    sys.exit(0)

if cmd == 'due':
    plan = due_plan(data)
    if json_out:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
    else:
        print('# agent-cron due plan\n')
        print(f"- store: `{plan['store']}`")
        print(f"- at: `{plan['at']}`")
        print('- mode: dry-run/read-only; no execution, push, scheduler, systemd, crontab, or state writes\n')
        if not plan['tasks']:
            print('No agent-cron tasks are defined.')
        else:
            print('| id | status | schedule | due | due count | missed | scheduled at | next due | lock |')
            print('|---|---|---|---:|---:|---:|---|---|---|')
            for t in plan['tasks']:
                print(f"| `{t['id']}` | `{t['status']}` | `{t['schedule']}` | {str(t['due']).lower()} | {t['dueCount']} | {t['missedRuns']} | `{t['scheduledAt'] or ''}` | `{t['nextDueAt'] or ''}` | `{t['lockState']}` |")
        if plan['errors']:
            print('\n## Errors')
            for e in plan['errors']:
                print(f'- {e}')
    sys.exit(0 if plan['ok'] else 1)

norm = normalize(data)
if json_out:
    print(json.dumps(norm, ensure_ascii=False, indent=2))
    sys.exit(0)

print('# agent-cron tasks\n')
print(f'- store: `{store}`')
print('- mode: store/list/due only; no execution, push, scheduler, systemd, crontab, or state writes\n')
if not norm['tasks']:
    print('No agent-cron tasks are defined.')
    sys.exit(0)
print('| id | schedule | enabled | notify | catch-up | tools | last status |')
print('|---|---|---:|---|---|---|---|')
for t in norm['tasks']:
    tools = ','.join(t['allowedTools']) if t['allowedTools'] else '(default)'
    print(f"| `{t['id']}` | `{t['schedule']}` | {str(t['enabled']).lower()} | `{t['notify']}` | `{t['catchUpPolicy']}` | `{tools}` | `{t.get('lastStatus') or 'unknown'}` |")
PY
