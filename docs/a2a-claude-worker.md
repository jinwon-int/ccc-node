# A2A Claude Code worker lane

`ccc-node` can be used in two layers on the same machine:

1. **Node harness** — Claude Code CLI, hooks, output style, status line, Telegram bridge, and local memory bootstrap.
2. **A2A worker analysis backend** — the broker poller service may keep its historical service name (`a2a-hermes-worker`), while the task analysis adapter is switched to Claude Code through the Nexus bridge script.

The service name is therefore not the source of truth. For A2A classification, verify the worker process environment and the broker `/workers` metadata.

## Reference shape

This is the active pattern used by the `nosuk` lane and the `soonwook` follow-up lane.

| Plane | Expected shape |
|---|---|
| Poller service | `a2a-hermes-worker` may remain the systemd service name |
| Worker root | `/opt/a2a-broker-worker` |
| Claude CLI | `A2A_CLAUDE_CODE_BIN=/usr/bin/claude` |
| Analysis adapter | `claude-a2a-analysis-bridge.mjs` |
| Metadata | `runtime=claude-code`, `harness=claude`, `adapter=claude-a2a-analysis-bridge` |
| Broker team routing | Team1 → Seoseo broker tunnel; Team2 → Gwakga broker tunnel |

Minimal non-secret env overlay:

```text
OPENCLAW_BIN=/opt/a2a-broker-worker/scripts/claude-a2a-analysis-bridge.mjs
A2A_OPENCLAW_ANALYSIS_BIN=/opt/a2a-broker-worker/scripts/claude-a2a-analysis-bridge.mjs
A2A_CLAUDE_CODE_BIN=/usr/bin/claude
WORKER_MODE=persistent
WORKER_METADATA_JSON={"runtime":"claude-code","harness":"claude","nodeId":"<node>","workspaceRoot":"/opt/a2a-broker-worker","adapter":"claude-a2a-analysis-bridge","modelProvider":"anthropic"}
```

Keep existing `WORKER_ID`, `WORKER_ROLE`, `A2A_HOME_BROKER_ID`, `BROKER_URL`, and safe workspace ids unless the migration explicitly changes team routing. If a node already has meaningful workspace ids, add the Claude lane id instead of replacing them, for example:

```text
WORKER_WORKSPACE_IDS=soonwook-claude-code,soonwook-validation,vps6-main
```

## Verification checklist

Run these checks before claiming the lane is converted:

```bash
# Node-side liveness and adapter wiring
systemctl is-active a2a-hermes-worker
PID=$(systemctl show -p MainPID --value a2a-hermes-worker)
tr '\0' '\n' < /proc/$PID/environ \
  | grep -E '^(WORKER_ID|WORKER_METADATA_JSON|WORKER_WORKSPACE_IDS|OPENCLAW_BIN|A2A_OPENCLAW_ANALYSIS_BIN|A2A_CLAUDE_CODE_BIN)='

# Claude CLI is present and authenticated; do not print credentials.
claude --version
claude auth status --text

# Deployed scripts parse.
node --check /opt/a2a-broker-worker/scripts/claude-a2a-analysis-bridge.mjs
node --check /opt/a2a-broker-worker/scripts/a2a-task-handler.mjs
```

Then verify the broker row on the correct broker:

- Team1 workers: Seoseo broker.
- Team2 workers: Gwakga broker.

The row should be online/fresh and expose metadata like:

```json
{
  "status": "online",
  "metadata": {
    "runtime": "claude-code",
    "harness": "claude",
    "adapter": "claude-a2a-analysis-bridge"
  }
}
```

## No-provider adapter smoke

Before any real provider canary, run the bridge with a fake Claude binary so the JSON contract, executable path, and Node runtime are proven without spending provider quota:

```bash
cat > /tmp/fake-claude-a2a <<'SH'
#!/usr/bin/env bash
python3 - <<'PY'
import json
analysis = {
  "status": "done",
  "summary": "fake claude bridge smoke ok",
  "findings": ["fake-cli invoked"],
  "risks": [],
  "recommendations": [],
  "evidenceRefs": ["fake://smoke"],
}
print(json.dumps({"result": json.dumps(analysis)}))
PY
SH
chmod 700 /tmp/fake-claude-a2a

A2A_CLAUDE_CODE_BIN=/tmp/fake-claude-a2a \
  node /opt/a2a-broker-worker/scripts/claude-a2a-analysis-bridge.mjs \
  agent --json --message "read-only fake smoke" --session-id smoke --timeout 5
```

Expected: the outer payload contains inner analysis JSON with `status=done`.

## Approval boundaries

Do **not** perform these as part of a documentation or adapter-wiring check without fresh operator approval:

- provider canary / real Claude send through an A2A task;
- broker task creation if the requested scope is read-only diagnosis;
- DB prune/migration/replay;
- manual ACK/replay;
- release/tag publish;
- secret movement or token printing.

## Worked node notes

- `nosuk`: Team1 lane, broker row `workspaceIds=["nosuk-claude-code"]`, metadata `runtime=claude-code`.
- `soonwook`: Team2 lane, existing validation workspace ids were preserved and `soonwook-claude-code` was added; broker row now reports `runtime=claude-code` while the poller service name remains `a2a-hermes-worker`.
