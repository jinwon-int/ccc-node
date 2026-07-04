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

## Termux native worker harness (PR-first slice)

For mobile nodes such as `gongyung` and `daegyo`, the first safe native slice is
additive: validate and launch `a2a-broker-worker/dist/worker.js` with the
Termux/glibc-runner Node wrapper, while keeping the historical proot worker as a
fallback until a later operator-approved cutover. Do not install Termux:Boot,
restart workers, stop proot, or create broker tasks from this repository-only
check.

The repository helper reads a systemd-style env file and fails closed unless the
worker and bridge wiring are explicitly native:

```bash
cp docs/examples/a2a-termux-native-worker.env.example /tmp/a2a-native-worker.env
# Edit /tmp/a2a-native-worker.env on the phone with real node-local paths.

scripts/a2a-termux-native-worker.sh check --env-file /tmp/a2a-native-worker.env
scripts/a2a-termux-native-worker.sh print-command --env-file /tmp/a2a-native-worker.env
```

Required shape:

- `A2A_TERMUX_NATIVE=1` so old proot/systemd env files are rejected.
- `A2A_NATIVE_NODE_BIN` points at the Termux native glibc-runner Node wrapper.
- `A2A_WORKER_ROOT/dist/worker.js` exists and is launched by that native Node.
- `A2A_CLAUDE_CODE_BIN` points at the native Claude wrapper.
- `OPENCLAW_BIN` and `A2A_OPENCLAW_ANALYSIS_BIN` both point at the same bridge
  file, one of:
  - `claude-a2a-analysis-bridge.mjs` — read-only analysis only, or
  - `claude-a2a-patch-bridge.mjs` — intent-aware superset (a2a-nexus #1021):
    identical analysis behavior plus a deterministic single-shot GitHub PATCH
    path. To enable single-shot, also set `A2A_CLAUDE_CODE_PATCH_MODE=single-shot`
    (the checker fails closed if this is set without the patch bridge).
- `BROKER_URL=http://127.0.0.1:18790` so the worker uses the local tunnel to the
  broker instead of embedding remote broker details in the launcher.
- `WORKER_MODE=persistent` and `WORKER_METADATA_JSON` includes
  `runtime=claude-code`, `harness=claude`, and an `adapter` that matches the
  wired bridge (`claude-a2a-analysis-bridge` or `claude-a2a-patch-bridge`).
- Env hygiene is enabled:
  `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, `DISABLE_GROWTHBOOK=1`, and
  `USE_BUILTIN_RIPGREP=0`.

The checker also fails closed when the exec'd wrappers (`A2A_NATIVE_NODE_BIN`,
`A2A_CLAUDE_CODE_BIN`) are not executable, when a `A2A_WORKER_SCRIPT` override
points at a `worker.js` outside `A2A_WORKER_ROOT` (path-escape), and — for
`run` — when the final `exec` itself fails (a bounded error, never a raw
traceback).

Only after review and fresh operator approval should the phone run:

```bash
scripts/a2a-termux-native-worker.sh run --env-file /path/outside/repo/a2a-native-worker.env
```

That command `exec`s native Node in the current process; supervision, the
`18790 -> broker:8787` tunnel, Termux:Boot wiring, and proot cutover remain a
separate live-ops step.

## Canonical supervisor + health check

The same script now owns the SSH tunnel + worker respawn loop that gongyung
and daegyo used to hand-roll under `~/.hermes/scripts/native-worker-supervisor.sh`.
Those local versions let a single Seoseo-broker restart snowball into a
7-supervisor pile-up on gongyung (Wiki ND-1236) because nothing enforced
singleton semantics and orphaned SSH tunnels kept the local port bound.

The canonical version fixes both:

- **Singleton via `flock`.** A second `supervise` invocation exits with
  rc=3 while another supervisor holds `$HOME/.a2a/a2a-native-worker-supervisor.lock`.
- **Orphan-safe cleanup.** `cleanup_orphans` sweeps parent=1 SSH tunnels on
  port 18790 at start; `kill_tree` walks child PIDs recursively at stop so
  the tunnel subshell and its ssh grandchild are torn down together.

```bash
# Foreground supervisor (SSH tunnel + worker respawn loop).
scripts/a2a-termux-native-worker.sh supervise --env-file /path/canonical.env

# Read-only snapshot: supervisor PID, tunnel UP/DOWN, worker count, orphans.
scripts/a2a-termux-native-worker.sh status

# SIGTERM the supervisor holding the lock (KILL fallback + tunnel sweep).
scripts/a2a-termux-native-worker.sh stop
```

Additional env keys the supervisor needs (on top of `check`/`run`):

- `A2A_TUNNEL_SSH_TARGET` — SSH host alias for the remote broker (required).
- `A2A_TUNNEL_REMOTE` — optional; defaults to `127.0.0.1:8787`.

Cron-safe health check (separate script so it doesn't need supervisor state):

```bash
# Default: self-heals by spawning a supervisor via `setsid -f` when none is
# holding the lock.  --no-self-heal makes it strictly read-only.
scripts/a2a-termux-native-worker-health.sh --env-file /path/canonical.env
scripts/a2a-termux-native-worker-health.sh --env-file /path/canonical.env --no-self-heal --json
```

Exit codes are distinct so cron logs are self-explanatory:

| rc | meaning                                                  |
|----|----------------------------------------------------------|
| 0  | healthy, or spawned a fresh supervisor                   |
| 2  | env validation failure / missing `--env-file`            |
| 3  | supervisor up but tunnel DOWN (self-heal can't help)     |
| 4  | supervisor-count-cap exceeded — MANUAL SWEEP (ND-1236)   |
| 5  | self-heal requested but `setsid` returned nonzero        |

The rc=4 path is a direct guard against the ND-1236 pile-up: the checker
counts distinct supervisor-looking processes (both the canonical script AND
the legacy `~/.hermes/scripts/native-worker-supervisor.sh` pattern) and
refuses to self-heal on top of an existing pile-up. `--max-supervisors N`
overrides the default cap of 1.

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
