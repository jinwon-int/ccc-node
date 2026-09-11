# Node status tool — JSON CLI and `family-ops` MCP (#1694)

One aggregated, read-only call replaces several shell probes: serving
checkout state, bridge service/transport health, and scheduler/task
occupancy. Built on the same structure as the skill lookup (#1678): a
stdlib-only common module, a JSON CLI, and an MCP tool sharing one stdio
implementation. This is the first tool from the #1694 roadmap; each further
tool lands separately.

## Components

| Path | Role |
|---|---|
| `bridge/core/node_status.py` | Common collectors + ssh aggregation. stdlib-only. |
| `bridge/core/family_ops_server.py` | `family-ops` stdio MCP server (`node_status`). |
| `bridge/core/mcp_stdio.py` | Shared MCP stdio framing/protocol (also used by `family-skills`). |
| `scripts/ccc-node-status.py` | JSON CLI; `stdout` results only, `stderr` diagnostics. |

## What one call returns

```json
{
  "observed_at": "2026-09-12T00:00:00Z",
  "node": "<hostname>",
  "repo_root": "/opt/ccc-node",
  "status": "ok",
  "sections": {
    "source":         {"head": "...", "branch": "main", "dirty": 0, "status": "ok", "latency_ms": 12},
    "bridge_process": {"running": true, "pid": 123, "status": "ok"},
    "service":        {"bot_status": "available", "service": "available", "telegram": "healthy",
                       "piri": "healthy", "turn_occupancy": "...", "healthy": true, "status": "ok"},
    "scheduler":      {"summary": {"total": 2, "due": 1}, "at": "...", "status": "ok"},
    "model":          {"status": "unknown", "reason": "no structured source"}
  }
}
```

Sources (all read-only, reused — nothing new is probed):

- `scripts/ccc-bridge-locate.sh --json` → serving checkout head/branch/dirty,
  bridge process/pid,
- `bridge/start.sh --path <project> --status` → service, transport
  (Telegram), provider-adjacent health (Piri), turn occupancy (parsed
  text; never request/message bodies),
- `scripts/agent-cron.sh status --json` → task occupancy summary.

Every section carries `status: ok|unknown` and, when ok, its latency. A
timeout, nonzero exit, unparseable or oversize output becomes `unknown`
with a short error — a failed collector never fabricates values and never
fails the whole call. Model identity has no structured source yet and is
reported as `unknown` (#1694 principle); it may become a real field when the
bridge exposes one.

## CLI and MCP

```bash
python3 scripts/ccc-node-status.py                  # this node
python3 scripts/ccc-node-status.py --node nosuk    # one peer over ssh
python3 scripts/ccc-node-status.py --node a --node b   # aggregate peers
```

MCP: the `family-ops` server exposes one tool, `node_status` (optional
`node` argument = peer ssh alias). Registered by `claude/mcp-setup.sh`
alongside `family-skills`; tool pattern `mcp__family-ops__*` pre-allowed in
`claude/settings.base.json`; injected by the bridge for the same owner
contexts as `family-skills`.

Remote aggregation preconditions: passwordless `ssh <alias>` to a peer that
runs this repo (default remote path `/opt/ccc-node/scripts/ccc-node-status.py`,
override `CCC_NODE_STATUS_REMOTE_PATH`); the ssh command itself can be
overridden via `CCC_NODE_STATUS_SSH`. A failed or unreachable peer is
node-scoped `unknown` — other nodes are unaffected. Host aliases are
validated (no shell metacharacters) and run with `BatchMode` +
`ConnectTimeout`.

## Policy and scope notes

- Same node policy gate as #1678: `external` isolation profiles and
  `shared` audiences are denied at call time from the server process
  environment; tool arguments cannot self-declare a context.
- Strictly read-only: no restarts, updates, scheduled work, or
  configuration changes. Status output informs decisions; it never
  substitutes for approvals or live re-verification at execution time.
- Collector paths can be redirected for tests via `CCC_NODE_STATUS_LOCATE`,
  `CCC_NODE_STATUS_BRIDGE_STATUS`, `CCC_NODE_STATUS_AGENT_CRON`
  (space-separated command strings).
- Remaining #1694 roadmap tools (`task_status`, `pr_readiness`,
  `deployment_diff`, `incident_find`) are intentionally absent here.
