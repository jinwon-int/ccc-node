---
description: List, validate, dry-run-resolve, inspect local locks, and preview/describe agent-cron task execution boundaries.
allowed-tools: Bash(/opt/ccc-node/scripts/agent-cron.sh:*)
---

## Live agent-cron store

!`/opt/ccc-node/scripts/agent-cron.sh list 2>&1`

## Dry-run due plan

!`/opt/ccc-node/scripts/agent-cron.sh due 2>&1 || true`

## Lock and run boundary

Task locks are local primitives for manual run-with-lock execution. Do not acquire or release locks from this summary command; only explain that `agent-cron.sh lock <task-id> --action probe --json` is the read-only inspection path when a specific task id is provided by the operator.

`agent-cron.sh run <task-id> --dry-run --json` is also read-only: it previews due/lock/headless/notification metadata but does not acquire locks, execute prompts, write history, write push spool files, install schedulers, or send messages.

`agent-cron.sh run <task-id> --json` is an explicit manual execution path for due enabled tasks. It acquires/releases the local task lock, invokes `headless.sh`, records `lastRunAt`/`lastStatus`/`lastRunId`, and when `notify=telegram-owner` writes a short redacted owner-only bridge spool file. It still does not directly send Telegram/provider messages, install schedulers, edit crontab/systemd, or touch remotes.

## Task

Summarize the configured agent-cron definitions for the operator in Korean using:

- confirmed facts;
- risks / missing definitions;
- execution boundary;
- next action.

Do not run tasks, acquire/release locks, write scheduler state, edit crontab/systemd, update `lastRunAt`, send Telegram/provider messages, or call non-dry-run `run` from this summary command. If the operator explicitly requests execution, use `run <task-id> --dry-run --json` first to show the boundary, then call non-dry-run `run` only for a due enabled task.
