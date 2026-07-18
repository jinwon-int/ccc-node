# Agent-cron operations

`agent-cron` is the local durable task scheduler surface for ccc-node. It is intentionally conservative: status and planning modes are read-only, and timer installation is a separate explicit step.

## Commands

- `scripts/agent-cron.sh list [--json]` — inspect configured tasks.
- `scripts/agent-cron.sh due [--at ISO8601] [--json]` — read-only due/retry resolver.
- `scripts/agent-cron.sh status [--at ISO8601] [--json]` — read-only operator rollup for task health, retry wait/exhaustion, lock state, and last run state.
- `scripts/agent-cron.sh run <task-id> --dry-run` — execution-plan preview only.
- `scripts/agent-cron.sh scheduler --dry-run` — one scheduler tick preview.
- `scripts/agent-cron.sh scheduler --execute` — explicit one-shot execution path for an already-approved scheduler unit.

## Headless runners

The installer defaults to the existing Claude runner. Use `--runner codex` to
install the ephemeral Codex runner instead. Codex defaults to
`CCC_CODEX_SANDBOX=read-only`, sets non-interactive approval policy to `never`,
and never persists a Codex session. Broader sandboxes require an explicit
`--codex-sandbox` choice at installation time.

Tasks may set `maxRuns` to a positive integer. For those bounded tasks, every
completed headless invocation, successful or failed, increments durable
`runCount`; reaching the limit disables the task and cancels any pending retry.
This makes `maxRuns: 1`
safe for one-time LLM jobs without leaving an annually recurring cron enabled.
Set `notBefore` to the intended UTC activation timestamp so a newly-created
annual cron expression cannot catch up an occurrence from the previous year.
For example, a July 22 one-time job uses its normal five-field schedule,
`notBefore: 2026-07-22T02:27:00Z`, and `maxRuns: 1`.

## Safety boundaries

Read-only/status modes never acquire locks, execute prompts, write bridge spools, install timers, edit crontab/systemd, send Telegram, call providers, or touch remotes. Execution mode may write task history and owner-only redacted spool entries, but still does not install timers or call Telegram/provider APIs directly.

## Source boundaries

- `schemas/agent-cron-task-store.schema.json` is the structural source of truth.
- `scripts/agent_cron_schema.py` applies the schema fail-closed without an optional
  system Python dependency; duplicate task IDs are the only store-level semantic
  rule layered on top.
- `scripts/agent_cron_model.py` owns pure task lookup and prompt-free list projections.
- `scripts/agent_cron_repository.py` owns validated load and private atomic writes.
- `scripts/agent_cron_lib.py` owns pure schedule and retry calculations.
- `scripts/agent_cron.py` is an import-safe CLI composition root. Dispatch only runs
  through `main()`; importing it does not parse commands, print, or mutate the
  filesystem or process environment.

Planning functions remain read-only and produce explicit mutation metadata. The
runner applies locks, headless execution, history, retry state, and spool writes only
after an explicit `run` or `scheduler --execute` dispatch.

## Fleet closeout pattern

For fleet operations, collect each node's `status --json` output into evidence blocks and summarize only metadata: node, task id, `lastStatus`, `retryEligibleAt`, retry exhaustion, lock state, and safe error class. Do not collect prompts, memory contents, raw env, tokens, chat IDs, or provider output.
