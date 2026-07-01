# Agent-cron operations

`agent-cron` is the local durable task scheduler surface for ccc-node. It is intentionally conservative: status and planning modes are read-only, and timer installation is a separate explicit step.

## Commands

- `scripts/agent-cron.sh list [--json]` — inspect configured tasks.
- `scripts/agent-cron.sh due [--at ISO8601] [--json]` — read-only due/retry resolver.
- `scripts/agent-cron.sh status [--at ISO8601] [--json]` — read-only operator rollup for task health, retry wait/exhaustion, lock state, and last run state.
- `scripts/agent-cron.sh run <task-id> --dry-run` — execution-plan preview only.
- `scripts/agent-cron.sh scheduler --dry-run` — one scheduler tick preview.
- `scripts/agent-cron.sh scheduler --execute` — explicit one-shot execution path for an already-approved scheduler unit.

## Safety boundaries

Read-only/status modes never acquire locks, execute prompts, write bridge spools, install timers, edit crontab/systemd, send Telegram, call providers, or touch remotes. Execution mode may write task history and owner-only redacted spool entries, but still does not install timers or call Telegram/provider APIs directly.

## Fleet closeout pattern

For fleet operations, collect each node's `status --json` output into evidence blocks and summarize only metadata: node, task id, `lastStatus`, `retryEligibleAt`, retry exhaustion, lock state, and safe error class. Do not collect prompts, memory contents, raw env, tokens, chat IDs, or provider output.
