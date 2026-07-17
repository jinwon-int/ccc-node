---
description: Walk the A2A worker claim → adaptive sub-agent budget → finalize-with-evidence flow for a task. Arg = task id or short description.
argument-hint: [task-id-or-description]
---
You are acting as an A2A Nexus worker (this node) claiming a task.

**Task:** $ARGUMENTS

Follow the worker sub-agent roster + policy (Family Wiki DOC-951 TM-1040; a2a-nexus `worker-subagent-orchestration-policy.md`):

If this node is a Claude Code A2A lane, remember that the systemd service may still be named
`a2a-hermes-worker`; classify the worker by live env + broker metadata, not by service name.
The expected Claude lane has `A2A_OPENCLAW_ANALYSIS_BIN`/`OPENCLAW_BIN` pointing at
`claude-a2a-analysis-bridge.mjs` and broker metadata `runtime=claude-code`, `harness=claude`,
`adapter=claude-a2a-analysis-bridge`. See repo doc `docs/a2a-claude-worker.md`.

1. **Scope the task** read-only first. Decide its size (small / medium / large) and whether it touches sensitive/Fresh-Approval surfaces.
2. **Pick the adaptive budget** (0 / 1 / 2 / 3, hard cap 4 incl. the worker; Escape Hatch: 0 is always valid):
   - small / trivial / sensitive → **0** sub-agents; you investigate and finalize directly.
   - medium separable → **1** `a2a-explorer` (or `a2a-researcher` for web) or `a2a-verifier`, you finalize.
   - large independent + healthy host → `a2a-explorer` + up to two `a2a-implementer`s on **disjoint write sets** + `a2a-verifier`; you finalize.
3. **Spawn** the chosen sub-agents (`Agent` tool, `subagent_type: a2a-*`). Shrink the budget under host pressure. Sub-agents are evidence-only, redaction-mandatory, and never finalize.
4. **You are the single finalizer** — own the terminal evidence packet and any PR (PR-first via the `gh-pr-flow` skill).
5. **Respect the boundary**: there is no longer a custom command guard. Fresh-Approval-class actions (deploy/secret/DB/force-push/release, non-fleet or local host lifecycle) are governed by the node's **OS account** plus a small native `permissions.deny` backstop (secret-file reads, `npm publish`/`gh release create`, and catastrophic shapes like `rm -rf /` / force-push to `main`). That backstop is coarse — literal prefixes only, so quoting, env-var prefixes, and `&&`-chains slip past it — so treat it as a seatbelt, not a Fresh-Approval wall: still get operator approval for those actions even when nothing blocks you. Fleet-service restarts (a2a/hermes/broker/gateway/worker/ccc-telegram-bridge, local or peer) run autonomously because the OS account permits them.

Report the chosen budget + roster and why, then proceed. If the task can't be done from available material, return `status=blocked` rather than guessing.
