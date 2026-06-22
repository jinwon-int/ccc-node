#!/usr/bin/env bash
# agent-cron — first-class durable task store/list/due surface.
#
# Issue #55 incremental slices:
# - store/list/validate is implemented.
# - due is a read-only dry-run resolver for schedule/catch-up planning.
#
# This script intentionally does not execute prompts, write push spool entries,
# install timers, mutate lastRunAt, or touch live cron/systemd state.
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
       agent-cron.sh run <task-id>

Implemented slices:
- list/validate: inspect and validate the task definition store.
- due: read-only dry-run schedule resolver. It reports due tasks, missed windows,
  catch-up policy, and lock paths, but never executes prompts or writes state.
- lock: local atomic task-lock acquire/release/probe primitives only. It writes
  lock files under the task store's sibling locks/ directory, but never executes
  prompts, sends notifications, installs schedulers, or updates task history.

No task execution, Telegram push, scheduler bootstrap, systemd/crontab writes,
provider sends, lastRunAt updates, or remote-node actions are performed.
EOF
      exit 0
      ;;
    *) break ;;
  esac
  shift
done

case "$CMD" in
  list|validate|due|lock) ;;
  run|execute|scheduler|install|enable|disable|add|remove)
    echo "agent-cron $CMD is not implemented in this read-only slice; no filesystem changes were made." >&2
    exit 2
    ;;
  *) echo "Unknown command: $CMD" >&2; exit 2 ;;
esac

export STORE JSON CMD AT
export EXTRA_ARGS="$*"
python3 - <<'PY'
import json
import os
import re
import shlex
import socket
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
    stale = bool(timeout and age is not None and age > timeout)
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
            'redactProfile': t.get('redactProfile', 'default'),
            'lastRunAt': t.get('lastRunAt'),
            'lastStatus': t.get('lastStatus'),
            'lastRunId': t.get('lastRunId'),
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


data, errors = load_doc()
if errors:
    for e in errors:
        print(f'agent-cron: {e}', file=sys.stderr)
    sys.exit(1)

if cmd == 'lock':
    result, as_json, rc = lock_command(data)
    emit_lock(result, as_json)
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
