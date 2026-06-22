---
description: ccc-node harness doctor — classify settings/hook/output-style/statusline/bridge drift; `--fix` and `--rollback` are dry-run, `--apply` writes only scoped settings changes after backup.
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

Do not run `--fix --apply` or `--rollback --apply` unless the operator explicitly approves a repair action. `--fix` and `--rollback` alone are dry-run only; apply modes currently touch only `settings.json` and still fail closed on manual/risky items.
