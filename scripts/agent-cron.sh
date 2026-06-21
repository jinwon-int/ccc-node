#!/usr/bin/env bash
# agent-cron — first-class durable task store/list surface.
#
# First slice for issue #55: define and inspect the task store only. This script
# intentionally does not execute prompts, write push spool entries, install
# timers, or mutate live cron/systemd state.
set -uo pipefail

STORE="${CCC_AGENT_CRON_STORE:-$HOME/.claude/state/agent-cron/tasks.json}"
CMD="${1:-list}"
[ $# -gt 0 ] && shift
JSON=0
while [ $# -gt 0 ]; do
  case "$1" in
    --json) JSON=1 ;;
    --store) [ -n "${2:-}" ] || { echo "--store requires a path" >&2; exit 2; }; STORE="$2"; shift ;;
    -h|--help)
      cat <<'EOF'
Usage: agent-cron.sh [list|validate] [--store PATH] [--json]
       agent-cron.sh run <task-id>

First implementation slice: store/list/validate only.
No task execution, Telegram push, scheduler bootstrap, systemd/crontab writes,
provider sends, or remote-node actions are performed.
EOF
      exit 0
      ;;
    *) break ;;
  esac
  shift
done

case "$CMD" in
  list|validate) ;;
  run|execute|scheduler|install|enable|disable|add|remove)
    echo "agent-cron $CMD is not implemented in this store/list-only slice; no filesystem changes were made." >&2
    exit 2
    ;;
  *) echo "Unknown command: $CMD" >&2; exit 2 ;;
esac

export STORE JSON CMD
python3 - <<'PY'
import json
import os
import re
import sys
from pathlib import Path

store = Path(os.environ['STORE']).expanduser()
json_out = os.environ.get('JSON') == '1'
cmd = os.environ['CMD']

ALLOWED_NOTIFY = {'none', 'telegram-owner'}
ALLOWED_PERMISSION = {'dontAsk', 'acceptEdits', 'default', None}
ID_RX = re.compile(r'^[A-Za-z0-9_.-]{1,96}$')


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
    return errors


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
            'lastRunAt': t.get('lastRunAt'),
            'lastStatus': t.get('lastStatus'),
            'lastRunId': t.get('lastRunId'),
        })
    out['tasks'].sort(key=lambda x: x['id'] or '')
    return out


data, errors = load_doc()
if errors:
    for e in errors:
        print(f'agent-cron: {e}', file=sys.stderr)
    sys.exit(1)

if cmd == 'validate':
    if json_out:
        print(json.dumps({'ok': True, 'store': str(store), 'tasks': len(data.get('tasks', []))}, ensure_ascii=False, indent=2))
    else:
        print(f'agent-cron store OK: {store} ({len(data.get("tasks", []))} task(s))')
    sys.exit(0)

norm = normalize(data)
if json_out:
    print(json.dumps(norm, ensure_ascii=False, indent=2))
    sys.exit(0)

print('# agent-cron tasks\n')
print(f'- store: `{store}`')
print('- mode: store/list only; no execution, push, scheduler, systemd, or crontab changes\n')
if not norm['tasks']:
    print('No agent-cron tasks are defined.')
    sys.exit(0)
print('| id | schedule | enabled | notify | tools | last status |')
print('|---|---|---:|---|---|---|')
for t in norm['tasks']:
    tools = ','.join(t['allowedTools']) if t['allowedTools'] else '(default)'
    print(f"| `{t['id']}` | `{t['schedule']}` | {str(t['enabled']).lower()} | `{t['notify']}` | `{tools}` | `{t.get('lastStatus') or 'unknown'}` |")
PY
