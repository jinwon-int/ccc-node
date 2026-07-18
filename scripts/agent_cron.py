#!/usr/bin/env python3
# agent_cron.py — Python implementation for the ccc-node agent-cron CLI.
#
# scripts/agent-cron.sh is intentionally kept as a compatibility wrapper for
# existing docs, commands, and systemd units. New code should target this Python
# entrypoint directly when possible.
import os as _os
import shlex as _shlex
import sys as _sys
from pathlib import Path as _Path

_USAGE = "Usage: agent-cron.sh [list|validate|status] [--store PATH] [--json]\n       agent-cron.sh due [--store PATH] [--at ISO8601] [--json]\n       agent-cron.sh lock <task-id> --action acquire|release|probe --run-id ID [--scheduled-at ISO8601] [--at ISO8601] [--json]\n       agent-cron.sh run <task-id> --dry-run [--at ISO8601] [--json]\n       agent-cron.sh scheduler --dry-run|--execute [--at ISO8601] [--max-runs N] [--json]\n\nImplemented slices:\n- list/validate: inspect and validate the task definition store.\n- due: read-only dry-run schedule resolver. It reports due tasks, missed windows,\n  catch-up policy, retryEligibleAt state, and lock paths, but never executes\n  prompts or writes state.\n- lock: local atomic task-lock acquire/release/probe primitives only. It writes\n  lock files under the task store's sibling locks/ directory, but never executes\n  prompts, sends notifications, installs schedulers, or updates task history.\n- run --dry-run: read-only execution-plan preview. It combines due, lock probe,\n  task policy, and headless command metadata, but never acquires locks, executes\n  prompts, sends notifications, installs schedulers, or updates task history.\n- scheduler --dry-run: read-only single-tick scheduler plan. It reports which\n  tasks would run or skip, including retry-due tasks, but never installs timers,\n  acquires locks, executes prompts, writes task state, or sends notifications.\n- scheduler --execute: explicit one-shot scheduler executor for approved live/systemd\n  use. It runs at most --max-runs due/retry-due tasks through the existing run path;\n  it never installs timers or edits crontab/systemd.\n- run: explicit manual execution for due enabled tasks. It acquires the task lock,\n  invokes ccc-headless, records lastRunAt/lastStatus/lastRunId, writes a\n  redacted owner-only bridge spool entry when notify=telegram-owner, appends a\n  bounded runHistory entry, records retryState/retryEligibleAt on failure, clears\n  retryState on success, and releases the lock in all normal failure/success\n  paths. It still does not call Telegram\n  or provider APIs, install schedulers, mutate crontab/systemd, or touch remotes.\n\nNo direct Telegram/API send, scheduler bootstrap, systemd/crontab writes,\nprovider sends, or remote-node actions are performed by agent-cron itself.\n"


def _die(message, code=2):
    print(message, file=_sys.stderr)
    raise SystemExit(code)


def _bootstrap_cli(argv=None):
    argv = list(_sys.argv[1:] if argv is None else argv)
    home = _Path(_os.environ.get('HOME', str(_Path.home()))).expanduser()
    store_value = _os.environ.get('CCC_AGENT_CRON_STORE') or str(home / '.claude' / 'state' / 'agent-cron' / 'tasks.json')
    cmd_value = argv.pop(0) if argv else 'list'
    json_value = False
    at_value = ''
    extra = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == '--json':
            json_value = True
        elif arg == '--store':
            i += 1
            if i >= len(argv) or not argv[i]:
                _die('--store requires a path')
            store_value = argv[i]
        elif arg == '--at':
            i += 1
            if i >= len(argv) or not argv[i]:
                _die('--at requires an ISO8601 timestamp')
            at_value = argv[i]
        elif arg in ('-h', '--help'):
            print(_USAGE, end='')
            raise SystemExit(0)
        else:
            extra = argv[i:]
            break
        i += 1

    if cmd_value not in ('list', 'validate', 'status', 'due', 'lock', 'run', 'scheduler'):
        if cmd_value in ('execute', 'install', 'enable', 'disable', 'add', 'remove'):
            _die(f'agent-cron {cmd_value} is not implemented in this read-only slice; no filesystem changes were made.', 2)
        _die(f'Unknown command: {cmd_value}', 2)

    return {
        'store': store_value,
        'json': json_value,
        'command': cmd_value,
        'at': at_value,
        'script_root': str(_Path(__file__).resolve().parents[1]),
        'extra_args': ' '.join(_shlex.quote(x) for x in extra),
    }

import json
import os
import re
import shlex
import socket
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Pure schedule + retry helpers live in agent_cron_lib (no side effects, unit
# tested directly). This composition root is also import-safe; only main()
# parses arguments and dispatches commands.
from agent_cron_lib import (  # noqa: E402
    OCCURRENCE_SCAN_LIMIT,
    parse_dt,
    parse_schedule,
    retry_view,
    apply_retry_transition,
    schedule_occurrences,
    next_after,
    fmt_dt,
)
from agent_cron_schema import validate_store  # noqa: E402
from agent_cron_model import normalize, task_by_id  # noqa: E402
from agent_cron_repository import (  # noqa: E402
    load_doc as load_store,
    write_doc as write_store,
)

script_root = Path(__file__).resolve().parents[1]
store = Path(
    os.environ.get('CCC_AGENT_CRON_STORE')
    or Path.home() / '.claude' / 'state' / 'agent-cron' / 'tasks.json'
).expanduser()
json_out = False
cmd = 'list'
at_raw = ''
extra_args = ''


def load_doc():
    return load_store(store)


def validate_doc(data):
    return validate_store(data)


def lock_path(task_id):
    base = store.parent if str(store.parent) != '.' else Path.cwd()
    return base / 'locks' / f'{task_id}.lock'


def boot_id():
    try:
        return Path('/proc/sys/kernel/random/boot_id').read_text(encoding='utf-8').strip()
    except OSError:
        return ''


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
    args = shlex.split(extra_args)
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


def due_plan(data):  # noqa: C901 -- #348 baseline hotspot
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
            'retryEligibleAt': None,
            'retryAttempt': None,
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
            spec = parse_schedule(task.get('schedule') or '', task.get('timezone', 'UTC'))
            row['scheduleKind'] = spec.get('kind', 'cron')
            last = parse_dt(task.get('lastRunAt'), f'tasks[{idx}].lastRunAt')
            anchor = parse_dt(task.get('anchorAt'), f'tasks[{idx}].anchorAt')
            occurrences, truncated = schedule_occurrences(spec, last, at, anchor)
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
            retry = retry_view(task, at)
            if retry:
                row['retryEligibleAt'] = retry.get('retryEligibleAt')
                row['retryAttempt'] = retry.get('retryAttempt')
                if not retry.get('valid', True):
                    row['retryError'] = retry.get('error')
                elif task.get('enabled') and not row['due']:
                    if retry.get('ready'):
                        row['due'] = True
                        row['dueCount'] = 1
                        row['scheduledAt'] = retry['state'].get('scheduledAt')
                        row['status'] = 'retry-due'
                        if row['lockState'] == 'held':
                            row['status'] = 'locked'
                        elif row['lockState'] == 'stale':
                            row['status'] = 'stale-lock'
                    elif retry.get('waiting'):
                        row['status'] = 'retry-wait'
                    elif retry.get('exhausted'):
                        row['status'] = 'retry-exhausted'
            row['nextDueAt'] = fmt_dt(next_after(spec, at, anchor))
        except Exception as e:
            row['status'] = 'invalid-schedule'
            row['error'] = str(e)
            errors.append(f'{tid}: {e}')
        rows.append(row)
    return {'ok': not errors, 'store': str(store), 'at': fmt_dt(at), 'mode': 'dry-run-read-only', 'tasks': rows, 'errors': errors}




def parse_scheduler_args():
    args = shlex.split(extra_args)
    dry_run = False
    execute = False
    at_value = at_raw
    local_json = json_out
    max_runs = 10
    i = 0
    while i < len(args):
        a = args[i]
        if a == '--dry-run':
            dry_run = True
        elif a == '--execute':
            execute = True
        elif a == '--json':
            local_json = True
        elif a == '--max-runs':
            i += 1
            if i >= len(args):
                raise ValueError('--max-runs requires a value')
            max_runs = int(args[i])
        elif a == '--at':
            i += 1
            if i >= len(args):
                raise ValueError('--at requires a value')
            at_value = args[i]
        else:
            raise ValueError(f'unsupported scheduler argument: {a}')
        i += 1
    if dry_run and execute:
        raise ValueError('scheduler accepts only one of --dry-run or --execute')
    if max_runs < 1 or max_runs > 100:
        raise ValueError('--max-runs must be an integer from 1 to 100')
    return dry_run, execute, at_value, local_json, max_runs


def agent_cron_status(data):
    plan = due_plan(data)
    tasks = []
    counts = {
        'total': 0,
        'healthy': 0,
        'due': 0,
        'retry_wait': 0,
        'retry_exhausted': 0,
        'failed': 0,
        'locked': 0,
        'disabled': 0,
        'invalid': 0,
    }
    for row in plan.get('tasks', []):
        status = row.get('status') or 'unknown'
        task_id = row.get('id')
        task = task_by_id(data, task_id) if task_id else None
        last_status = task.get('lastStatus') if isinstance(task, dict) else None
        health = 'healthy'
        if status == 'disabled':
            health = 'disabled'
        elif status in {'locked', 'stale-lock'}:
            health = 'locked'
        elif status == 'retry-exhausted':
            health = 'retry-exhausted'
        elif status == 'retry-wait':
            health = 'retry-wait'
        elif status in {'due', 'retry-due'}:
            health = 'due'
        elif status == 'invalid-schedule' or row.get('error'):
            health = 'invalid'
        elif last_status in {'failed', 'error'}:
            health = 'failed'
        counts['total'] += 1
        if health == 'retry-wait':
            counts['retry_wait'] += 1
        elif health == 'retry-exhausted':
            counts['retry_exhausted'] += 1
        else:
            counts[health] = counts.get(health, 0) + 1
        tasks.append({
            'id': task_id,
            'node': os.environ.get('CCC_NODE') or socket.gethostname(),
            'health': health,
            'status': status,
            'enabled': row.get('enabled'),
            'lastStatus': last_status,
            'lastRunAt': task.get('lastRunAt') if isinstance(task, dict) else None,
            'lastRunId': task.get('lastRunId') if isinstance(task, dict) else None,
            'scheduledAt': row.get('scheduledAt'),
            'nextDueAt': row.get('nextDueAt'),
            'retryEligibleAt': row.get('retryEligibleAt'),
            'retryAttempt': row.get('retryAttempt'),
            'retryExhausted': health == 'retry-exhausted',
            'lockState': row.get('lockState'),
            'lockPath': row.get('lockPath'),
            'error': row.get('error'),
        })
    return {
        'ok': plan.get('ok', False) and counts['invalid'] == 0,
        'mode': 'status-read-only',
        'store': str(store),
        'at': plan.get('at'),
        'summary': counts,
        'tasks': tasks,
        'errors': plan.get('errors', []),
        'mutations': mutation_flags(False, False, False),
    }


def emit_status(result, as_json):
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    print('# agent-cron status\n')
    print(f"- store: `{result.get('store')}`")
    print(f"- at: `{result.get('at') or ''}`")
    print('- mode: read-only; no task execution, lock acquire, spool write, scheduler install, or state writes\n')
    print('| id | health | last status | retry | next due | lock |')
    print('|---|---|---|---|---|---|')
    for t in result.get('tasks', []):
        retry = t.get('retryEligibleAt') or ('exhausted' if t.get('retryExhausted') else '')
        print(f"| `{t.get('id')}` | `{t.get('health')}` | `{t.get('lastStatus') or ''}` | `{retry}` | `{t.get('nextDueAt') or ''}` | `{t.get('lockState')}` |")


def scheduler_actions(plan):
    actions = []
    for row in plan.get('tasks', []):
        status = row.get('status')
        lock_state = row.get('lockState')
        action = 'skip'
        reason = status or 'unknown'
        if not row.get('enabled'):
            reason = 'disabled'
        elif row.get('due') and lock_state == 'held':
            reason = 'locked'
        elif row.get('due'):
            action = 'would-run'
            reason = status or 'due'
        elif status == 'retry-wait':
            reason = 'retry-wait'
        elif status == 'retry-exhausted':
            reason = 'retry-exhausted'
        else:
            reason = 'not-due'
        actions.append({
            'taskId': row.get('id'),
            'action': action,
            'reason': reason,
            'status': status,
            'scheduledAt': row.get('scheduledAt'),
            'dueCount': row.get('dueCount', 0),
            'missedRuns': row.get('missedRuns', 0),
            'retryEligibleAt': row.get('retryEligibleAt'),
            'retryAttempt': row.get('retryAttempt'),
            'lockState': lock_state,
            'lockPath': row.get('lockPath'),
        })
    return actions


def scheduler_plan(data):
    global at_raw
    try:
        dry_run, execute, at_value, as_json, max_runs = parse_scheduler_args()
        if not dry_run and not execute:
            return {
                'ok': False,
                'mode': 'scheduler-blocked',
                'store': str(store),
                'at': at_value or at_raw,
                'error': 'scheduler requires --dry-run or --execute; no filesystem changes were made',
                'mutations': mutation_flags(False, False, False),
            }, as_json, 2
        old_at = at_raw
        at_raw = at_value or at_raw
        try:
            plan = due_plan(data)
        finally:
            at_raw = old_at
        actions = scheduler_actions(plan)
        if dry_run:
            return {
                'ok': plan.get('ok', False),
                'mode': 'scheduler-dry-run-read-only',
                'store': str(store),
                'at': plan.get('at'),
                'actions': actions,
                'errors': plan.get('errors', []),
                'mutations': mutation_flags(False, False, False),
            }, as_json, 0 if plan.get('ok', False) else 1
        return scheduler_execute(data, plan, actions, at_value, as_json, max_runs)
    except Exception as e:
        return {
            'ok': False,
            'mode': 'scheduler-error',
            'store': str(store),
            'error': str(e),
            'mutations': mutation_flags(False, False, False),
        }, json_out, 1


def scheduler_execute(data, plan, actions, at_value, as_json, max_runs):
    runnable = [a for a in actions if a.get('action') == 'would-run' and a.get('taskId')]
    selected = runnable[:max_runs]
    results = []
    any_headless = False
    any_spool = False
    any_history = False
    any_lock = False
    for action in selected:
        result, _json, _rc = run_execute(data, action['taskId'], at_value or plan.get('at'), True)
        results.append(result)
        m = result.get('mutations') or {}
        any_headless = any_headless or bool(m.get('headlessExecute'))
        any_spool = any_spool or bool(m.get('pushSpoolWrite'))
        any_history = any_history or bool(m.get('historyAppend'))
        any_lock = any_lock or bool(m.get('lockAcquire'))
    return {
        'ok': True,
        'mode': 'scheduler-execute-one-shot',
        'store': str(store),
        'at': plan.get('at'),
        'plannedActions': len(actions),
        'runnableActions': len(runnable),
        'executedActions': len(results),
        'maxRuns': max_runs,
        'truncated': len(runnable) > len(selected),
        'results': results,
        'errors': plan.get('errors', []),
        'mutations': mutation_flags(any_lock, bool(results), any_headless, any_spool, any_history),
    }, as_json, 0


def emit_scheduler(result, as_json):
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print('# agent-cron scheduler dry-run plan\n')
        print(f"- store: `{result.get('store')}`")
        print(f"- at: `{result.get('at') or ''}`")
        print('- mode: dry-run/read-only; no lock acquire, execution, spool write, scheduler install, systemd/crontab, or state writes\n')
        if result.get('error'):
            print(f"error: {result['error']}")
        else:
            print('| task | action | reason | scheduled at | lock |')
            print('|---|---|---|---|---|')
            for a in result.get('actions', []):
                print(f"| `{a.get('taskId')}` | `{a.get('action')}` | `{a.get('reason')}` | `{a.get('scheduledAt') or ''}` | `{a.get('lockState')}` |")

def parse_run_args():
    args = shlex.split(extra_args)
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
    headless_cmd = os.environ.get('CCC_HEADLESS_CMD') or str(script_root / 'claude' / 'headless.sh')
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
    write_store(store, data)


def history_attempt(task, scheduled_at):
    attempts = 0
    retry_state = task.get('retryState') if isinstance(task, dict) else None
    if isinstance(retry_state, dict) and retry_state.get('scheduledAt') == scheduled_at:
        attempts = max(attempts, int(retry_state.get('attempt') or 0))
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
    one_shot_disabled = False
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
        attempt = history_attempt(task, scheduled_at)
        entry = {
            'runId': run_id,
            'scheduledAt': scheduled_at,
            'startedAt': fmt_dt(at),
            'finishedAt': fmt_dt(at),
            'status': status,
            'exitCode': headless.get('exitCode') if isinstance(headless, dict) else None,
            'attempt': attempt,
            'notifyState': notify_state,
        }
        append_run_history(task, entry)
        retry = apply_retry_transition(task, scheduled_at, attempt, run_id, status, at)
        task['lastRunAt'] = scheduled_at
        task['lastStatus'] = status
        task['lastRunId'] = run_id
        if status == 'success' and not task.get('keepAfterRun'):
            try:
                spec = parse_schedule(task.get('schedule') or '', task.get('timezone', 'UTC'))
                if spec.get('kind') == 'once':
                    task['enabled'] = False
                    one_shot_disabled = True
            except Exception:
                pass
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
        'oneShotDisabled': one_shot_disabled,
        'retry': retry,
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


def emit_due(plan, as_json):
    if as_json:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return
    print('# agent-cron due plan\n')
    print(f"- store: `{plan['store']}`")
    print(f"- at: `{plan['at']}`")
    print('- mode: dry-run/read-only; no execution, push, scheduler, systemd, crontab, or state writes\n')
    if not plan['tasks']:
        print('No agent-cron tasks are defined.')
    else:
        print('| id | status | schedule | due | due count | missed | scheduled at | next due | lock |')
        print('|---|---|---|---:|---:|---:|---|---|---|')
        for task in plan['tasks']:
            print(f"| `{task['id']}` | `{task['status']}` | `{task['schedule']}` | {str(task['due']).lower()} | {task['dueCount']} | {task['missedRuns']} | `{task['scheduledAt'] or ''}` | `{task['nextDueAt'] or ''}` | `{task['lockState']}` |")
    if plan['errors']:
        print('\n## Errors')
        for error in plan['errors']:
            print(f'- {error}')


def emit_list(data, as_json):
    norm = normalize(data)
    if as_json:
        print(json.dumps(norm, ensure_ascii=False, indent=2))
        return
    print('# agent-cron tasks\n')
    print(f'- store: `{store}`')
    print('- mode: store/list/due only; no execution, push, scheduler, systemd, crontab, or state writes\n')
    if not norm['tasks']:
        print('No agent-cron tasks are defined.')
        return
    print('| id | schedule | enabled | notify | catch-up | tools | last status |')
    print('|---|---|---:|---|---|---|---|')
    for task in norm['tasks']:
        tools = ','.join(task['allowedTools']) if task['allowedTools'] else '(default)'
        print(f"| `{task['id']}` | `{task['schedule']}` | {str(task['enabled']).lower()} | `{task['notify']}` | `{task['catchUpPolicy']}` | `{tools}` | `{task.get('lastStatus') or 'unknown'}` |")


def _dispatch(data):
    if cmd == 'lock':
        result, as_json, rc = lock_command(data)
        emit_lock(result, as_json)
        return rc
    if cmd == 'scheduler':
        result, as_json, rc = scheduler_plan(data)
        emit_scheduler(result, as_json)
        return rc
    if cmd == 'run':
        result, as_json, rc = run_dry_plan(data)
        emit_run(result, as_json)
        return rc
    if cmd == 'status':
        result = agent_cron_status(data)
        emit_status(result, json_out)
        return 0 if result.get('ok') else 1
    if cmd == 'validate':
        if json_out:
            print(json.dumps({'ok': True, 'store': str(store), 'tasks': len(data.get('tasks', []))}, ensure_ascii=False, indent=2))
        else:
            print(f'agent-cron store OK: {store} ({len(data.get("tasks", []))} task(s))')
        return 0
    if cmd == 'due':
        plan = due_plan(data)
        emit_due(plan, json_out)
        return 0 if plan['ok'] else 1
    emit_list(data, json_out)
    return 0


def main(argv=None):
    global at_raw, cmd, extra_args, json_out, script_root, store
    config = _bootstrap_cli(argv)
    store = Path(config['store']).expanduser()
    json_out = bool(config['json'])
    cmd = config['command']
    at_raw = config['at']
    script_root = Path(config['script_root'])
    extra_args = config['extra_args']

    data, errors = load_doc()
    if errors:
        for error in errors:
            print(f'agent-cron: {error}', file=sys.stderr)
        return 1
    return _dispatch(data)


if __name__ == '__main__':
    raise SystemExit(main())
