---
name: ccc-node-status
description: Inspect the current ccc-node source, serving bridge, provider readiness, workload, scheduler, and recent body-free diagnostics. Use for node status checks, bridge health questions, deployment preflight, or verifying that a node remained healthy after a scoped change.
---

# CCC Node Status

1. Locate the serving checkout:

   ```bash
   ROOT="${CCC_NODE_ROOT:-/opt/ccc-node}"
   bash "$ROOT/scripts/ccc-bridge-locate.sh" --json
   ```

2. From the reported checkout, collect only read-only state:

   ```bash
   git -C "$ROOT" status --short --branch
   git -C "$ROOT" log -1 --oneline
   bash "$ROOT/bridge/start.sh" --path "${CCC_BRIDGE_DEFAULT_PATH:-$HOME}" --status
   bash "$ROOT/scripts/agent-cron.sh" status --json
   ```

3. Read the health summary, not request or message bodies. Report source
   revision/cleanliness, active provider, service/transport/provider health,
   workload, and any separate A2A worker state.

4. Separate confirmed facts, warnings, risks, and next action. Never restart,
   update, run scheduled work, send a canary, or mutate configuration from a
   status request.
