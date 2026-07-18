# Agent-cron operations

`agent-cron` is the local durable task scheduler surface for ccc-node. It is intentionally conservative: status and planning modes are read-only, and timer installation is a separate explicit step.

## Commands

- `scripts/agent-cron.sh list [--json]` — inspect configured tasks.
- `scripts/agent-cron.sh due [--at ISO8601] [--json]` — read-only due/retry resolver.
- `scripts/agent-cron.sh status [--at ISO8601] [--json]` — read-only operator rollup for task health, retry wait/exhaustion, lock state, and last run state.
- `scripts/agent-cron.sh run <task-id> --dry-run` — execution-plan preview only.
- `scripts/agent-cron.sh scheduler --dry-run` — one scheduler tick preview.
- `scripts/agent-cron.sh scheduler --execute` — explicit one-shot execution path for an already-approved scheduler unit.

## Schedule forms

`schedule` accepts four kinds (epic #584-adjacent cron upgrade, referencing the
Hermes and OpenClaw schedulers):

- **Cron:** 5-field expression or `@hourly|@daily|@weekly|@monthly|@yearly`,
  matched in the task's `timezone` (IANA name, e.g. `Asia/Seoul`; default UTC).
- **Interval:** `every <N>m|h|d` (min 1 minute, max 366 days). Free-running from
  `lastRunAt`; set `anchorAt` (ISO8601) to phase-anchor occurrences
  (e.g. anchor `..T00:15Z` + `every 1h` fires at :15). A never-run interval task
  with no anchor is due immediately once.
- **One-shot:** `at <ISO8601>` or a bare ISO8601 timestamp. Naive timestamps are
  anchored to the task `timezone`. After a successful run the task is
  auto-disabled unless `keepAfterRun: true`.
- Unknown timezones and malformed expressions fail closed as
  `invalid-schedule` in `due`/`status` output; `due` rows expose `scheduleKind`.

## Payload kinds

Each task runs one payload (default: `prompt`, backward compatible):

- **prompt** (default): the existing headless Claude run of `prompt` via
  `claude/headless.sh`. Optional `payload.model` is passed through as
  `--model` (via `CCC_MODEL`). Wall-clock timeout `payload.timeoutSec`
  (default 3600s).
- **command**: `payload.argv` runs directly (no shell interpolation, no LLM
  token spend — watchdog/maintenance jobs). Optional `cwd`,
  `timeoutSec` (default 600s), `outputMaxBytes` (default 64 KiB, capped
  capture). `model` is rejected for command payloads.

A timed-out run records status `timeout` (exit code 124) and consumes the
normal retry policy. Cross-field payload rules (argv required for command,
argv/cwd rejected for prompt) are enforced fail-closed by `validate` and on
load.

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
