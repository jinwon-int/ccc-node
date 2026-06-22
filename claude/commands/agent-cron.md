---
description: List, validate, dry-run-resolve, inspect local locks, and preview run plans for agent-cron task definitions without executing tasks.
allowed-tools: Bash(/opt/ccc-node/scripts/agent-cron.sh:*)
---

## Live agent-cron store

!`/opt/ccc-node/scripts/agent-cron.sh list 2>&1`

## Dry-run due plan

!`/opt/ccc-node/scripts/agent-cron.sh due 2>&1 || true`

## Lock and run-plan boundary

Task locks are local primitives for future run-with-lock slices. Do not acquire or release locks from this summary command; only explain that `agent-cron.sh lock <task-id> --action probe --json` is the read-only inspection path when a specific task id is provided by the operator.

`agent-cron.sh run <task-id> --dry-run --json` is also read-only: it previews due/lock/headless/notification metadata but does not acquire locks, execute prompts, write history, write push spool files, install schedulers, or send messages.

## Task

Summarize the configured agent-cron definitions for the operator in Korean using:

- confirmed facts;
- risks / missing definitions;
- execution boundary;
- next action.

Do not run tasks, write scheduler state, edit crontab/systemd, update `lastRunAt`, send Telegram/provider messages, or call `run` unless a future implementation slice explicitly adds that behavior and the operator approves the relevant live action.
