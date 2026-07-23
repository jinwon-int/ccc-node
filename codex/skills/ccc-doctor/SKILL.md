---
name: ccc-doctor
description: Diagnose ccc-node harness, provider, bridge, memory, and managed-asset drift with the provider-aware doctor. Use when the operator asks for a health check, doctor report, readiness diagnosis, drift classification, or a preview of repairable ccc-node configuration.
---

# CCC Doctor

1. Resolve the serving checkout before making operational claims:

   ```bash
   ROOT="${CCC_NODE_ROOT:-/opt/ccc-node}"
   bash "$ROOT/scripts/ccc-bridge-locate.sh" --json
   ```

2. Run the doctor read-only and prefer its JSON output when available:

   ```bash
   bash "$ROOT/scripts/ccc-doctor.sh" --json
   ```

3. Report confirmed facts, drift, risks, and the smallest next action. Treat
   dormant assets for another provider as non-blocking unless the doctor marks
   them active.

4. Preview a repair with `--fix` only. Run `--fix --apply` or a rollback apply
   only after explicit operator approval, and preserve the generated backup.

Never print config bodies, credentials, environment values, or session content.
