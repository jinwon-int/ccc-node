---
description: ccc-node harness doctor — classify settings/hook/output-style/statusline/bridge drift; supports local diagnostics and read-only fleet-matrix summarization; `--fix` and `--rollback` are dry-run, `--apply` writes only scoped settings or explicitly scoped file repairs after backup.
allowed-tools: Bash(/opt/ccc-node/scripts/ccc-doctor.sh:*), Bash(/opt/ccc-node/scripts/ccc-doctor-fleet-matrix.sh:*)
---

## Live diagnostics

!`/opt/ccc-node/scripts/ccc-doctor.sh 2>&1`

## Task

Summarize the doctor result for the operator in Korean using the structured report format:

- confirmed facts;
- drift / warnings;
- risks;
- next action.

A node whose checkout is not `/opt/ccc-node` (or whose harness dir is not `/root/.claude`) holds installed files that setup.sh rewrote for that node; the report shows this as a `정상` `canonical path rewrite` row and compares through it. Installed files differing only by those paths are NOT drift — report them as clean.

Do not run `--fix --apply` or `--rollback --apply` unless the operator explicitly approves a repair action. `--fix` and `--rollback` alone are dry-run only. Apply modes currently touch only scoped `settings.json` repairs by default; file reinstall requires explicit `--scope=files` and still fails closed on symlink/path/plugin/manual/risky items.
