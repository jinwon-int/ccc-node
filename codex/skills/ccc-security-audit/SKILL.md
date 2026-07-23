---
name: ccc-security-audit
description: Run and interpret the read-only ccc-node security audit for permissions, execution policy, scanner integrity, redaction, spool/cache safety, and collected fleet evidence. Use when asked to audit security, check owner-only storage, inspect policy drift, or assess a node without making repairs.
---

# CCC Security Audit

1. Run the repository audit without repair flags:

   ```bash
   ROOT="${CCC_NODE_ROOT:-/opt/ccc-node}"
   bash "$ROOT/scripts/ccc-security-audit.sh" --json
   ```

2. If a fleet summary is requested, use the existing collected-evidence
   matrix only; do not infer live peer state:

   ```bash
   bash "$ROOT/scripts/ccc-security-audit-fleet-matrix.sh" --json
   ```

3. Report confirmed controls, warnings, risks, the security boundary, and the
   smallest next action. Do not reveal matched content, tokens, config bodies,
   raw environment, or private paths.

4. Do not apply repairs from this skill. Obtain explicit operator approval for
   any later mutation, deployment, restart, or provider/Telegram canary.
