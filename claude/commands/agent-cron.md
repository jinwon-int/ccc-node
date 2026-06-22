---
description: List, validate, and dry-run-resolve local agent-cron task definitions without executing them.
allowed-tools: Bash(/opt/ccc-node/scripts/agent-cron.sh:*)
---

## Live agent-cron store

!`/opt/ccc-node/scripts/agent-cron.sh list 2>&1`

## Dry-run due plan

!`/opt/ccc-node/scripts/agent-cron.sh due 2>&1 || true`

## Task

Summarize the configured agent-cron definitions for the operator in Korean using:

- confirmed facts;
- risks / missing definitions;
- execution boundary;
- next action.

Do not run tasks, write scheduler state, edit crontab/systemd, update `lastRunAt`, send Telegram/provider messages, or call `run` unless a future implementation slice explicitly adds that behavior and the operator approves the relevant live action.
