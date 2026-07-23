---
name: ccc-agent-cron
description: Inspect, validate, preview, and safely manage ccc-node agent-cron task definitions and execution boundaries. Use when the operator asks about scheduled agent work, due tasks, locks, retries, adding or changing a task, or previewing a scheduler tick.
---

# CCC Agent Cron

Set `ROOT="${CCC_NODE_ROOT:-/opt/ccc-node}"` and use
`"$ROOT/scripts/agent-cron.sh"`.

## Read-only inspection

```bash
bash "$ROOT/scripts/agent-cron.sh" list --json
bash "$ROOT/scripts/agent-cron.sh" status --json
bash "$ROOT/scripts/agent-cron.sh" due --json
bash "$ROOT/scripts/agent-cron.sh" scheduler --dry-run --json
```

For a named task, preview with:

```bash
bash "$ROOT/scripts/agent-cron.sh" run <task-id> --dry-run --json
bash "$ROOT/scripts/agent-cron.sh" lock <task-id> --action probe --json
```

## Mutation boundary

- `add`, `remove`, `enable`, and `disable` mutate only the task store; confirm
  the exact task definition before applying.
- A non-dry-run `run` may execute a due task and write history or a notification
  spool. Do it only when explicitly requested.
- `scheduler --execute` and timer installation are separate execution or host
  scheduling actions. Never infer approval from a status request.

Report configured tasks, due/retry state, lock state, execution effects, risks,
and the next action. Do not print prompt bodies or run history content.
