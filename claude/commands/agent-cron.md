---
description: List, validate, dry-run-resolve, inspect local locks, manage task definitions (add/remove/enable/disable), and preview/describe agent-cron task execution boundaries.
allowed-tools: Bash(/opt/ccc-node/scripts/agent-cron.sh:*)
---

## Live agent-cron store

!`/opt/ccc-node/scripts/agent-cron.sh list 2>&1`

## Dry-run due plan

!`/opt/ccc-node/scripts/agent-cron.sh due 2>&1 || true`

## Read-only status rollup

!`/opt/ccc-node/scripts/agent-cron.sh status 2>&1 || true`

## Dry-run scheduler tick plan

!`/opt/ccc-node/scripts/agent-cron.sh scheduler --dry-run 2>&1 || true`

## Lock and run boundary

Task locks are local primitives for manual run-with-lock execution. Do not acquire or release locks from this summary command; only explain that `agent-cron.sh lock <task-id> --action probe --json` is the read-only inspection path when a specific task id is provided by the operator.

`agent-cron.sh run <task-id> --dry-run --json` is also read-only: it previews due/lock/headless/notification metadata but does not acquire locks, execute prompts, write history, write push spool files, install schedulers, or send messages.

`agent-cron.sh run <task-id> --json` is an explicit manual execution path for due enabled tasks. It acquires/releases the local task lock, invokes `headless.sh`, appends bounded `runHistory`, records `lastRunAt`/`lastStatus`/`lastRunId`, persists bounded `retryState`/`retryEligibleAt` on failure when `retryPolicy` allows another attempt, clears retry state on success, and when `notify=telegram-owner` writes a short redacted owner-only bridge spool file. It still does not directly send Telegram/provider messages, install schedulers, edit crontab/systemd, or touch remotes.

`agent-cron.sh scheduler --dry-run --json` is read-only: it reports one deterministic scheduler tick (`would-run` / `skip`) using the due plan, retry state, and lock probe results. It does not acquire locks, execute prompts, write task history, write push spool files, install timers, edit crontab/systemd, or send messages.

## Task definition management

`agent-cron.sh add <task-id> --schedule EXPR --prompt TEXT [flags] --json` creates a task after schema + semantic + schedule validation (atomic private write, no execution). Schedules: 5-field cron/@shorthand (matched in `--timezone`, IANA), `every <N>m|h|d`, or one-shot `at <ISO8601>` (auto-disabled after success unless `--keep-after-run`). Payloads: default headless prompt (optional `--model`, `--timeout-sec`), or a no-LLM command via repeated `--argv` words (optional `--cwd`, `--output-max-bytes`). Notify modes: `none`, `telegram-owner`, `telegram-owner-on-failure`. `remove|enable|disable <task-id> --json` mutate only the store; they never execute, install timers, or send messages.

`agent-cron.sh scheduler --execute --json` is the approved one-shot executor path for live/systemd use: it consumes due/retry-due tasks through the same locked `run` path, so it may acquire locks, execute headless, write task history, and write owner-only redacted spool files. It still does not install timers, edit crontab/systemd, or directly call Telegram/provider APIs. Timer installation is handled separately by `scripts/install-agent-cron-systemd.sh --apply` and requires explicit approval.

## Task

Summarize the configured agent-cron definitions for the operator in Korean using:

- confirmed facts;
- risks / missing definitions;
- execution boundary;
- next action.

Do not run tasks, acquire/release locks, write scheduler state, edit crontab/systemd, update `lastRunAt`, send Telegram/provider messages, or call non-dry-run `run` from this summary command. If the operator explicitly requests execution, use `run <task-id> --dry-run --json` first to show the boundary, then call non-dry-run `run` only for a due enabled task. If the operator asks about scheduling, use `scheduler --dry-run --json`; `scheduler --execute` and actual timer installation remain explicit-approval operations.
