---
description: Read-only ccc-node harness doctor — classify settings/hook/output-style/statusline/bridge drift without mutating files.
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

Do not run `--fix` unless the operator explicitly approves a future repair slice. The current doctor implementation is read-only and `--fix` is intentionally not implemented.
