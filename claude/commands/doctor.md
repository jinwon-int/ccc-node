---
description: ccc-node harness doctor — classify settings/hook/output-style/statusline/bridge drift; `--fix` is dry-run, `--fix --apply` repairs only safe settings drift after backup.
allowed-tools: Bash(/opt/ccc-node/scripts/ccc-doctor.sh:*)
---

## Live diagnostics

!`/opt/ccc-node/scripts/ccc-doctor.sh 2>&1`

## Task

Summarize the doctor result for the operator in Korean using the structured report format:

- confirmed facts;
- drift / warnings;
- risks;
- next action.

Do not run `--fix --apply` unless the operator explicitly approves a repair action. `--fix` alone is dry-run only; `--fix --apply` currently repairs only deterministic `settings.json` drift after a backup tar and still fails closed on manual/risky items.
