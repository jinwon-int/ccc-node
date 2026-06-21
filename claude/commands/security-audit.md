---
description: Read-only ccc-node security audit — classify permissions, settings allowlist, scanner integrity, and spool/cache redaction without printing secrets.
allowed-tools: Bash(/opt/ccc-node/scripts/ccc-security-audit.sh:*)
---

## Live security audit

!`/opt/ccc-node/scripts/ccc-security-audit.sh 2>&1`

## Task

Summarize the security audit result for the operator in Korean using the structured report format:

- confirmed facts;
- warnings / risks;
- security boundary;
- next action.

Do not print raw secrets or matched file contents. Do not run `--fix` unless the operator explicitly approves a future repair slice. The current implementation is read-only and `--fix` is intentionally not implemented.
