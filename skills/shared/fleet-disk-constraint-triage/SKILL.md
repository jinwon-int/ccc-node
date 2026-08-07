---
name: fleet-disk-constraint-triage
description: Audit disk usage across a fleet, classify severity, identify root causes per node, and delegate cleanup to node-local agents with durable Wiki tickets instead of centralized remote deletes. Use when a rollout/update is blocked by low disk, when doing periodic fleet health checks, or when a node reports "no space" — especially mixed fleets with Android/Termux nodes where df / misreports capacity.
---

# fleet-disk-constraint-triage

Fleet-wide disk triage: measure correctly (platform-aware), classify severity,
find root causes, then **delegate cleanup to each node's local agent via durable
tickets** — never bulk-delete remotely from the central node.

## When to Use

- Periodic fleet health checks to catch disk capacity issues early
- Blocking conditions for updates or rollouts (need free space)
- Before major feature deployments or data accumulations
- Preparing for phase transitions or service shutdown (cleanup evidence)

## Step 1 — Audit disk usage (platform-aware)

- Linux/VPS nodes: `df -h /` (overall) + `du -sh ~/* 2>/dev/null | sort -rh | head -10`
- **Android/Termux nodes: NEVER judge by `df /`** — on Android, `/` is the
  read-only *system* partition (often shows ~0 free) and does not reflect app
  storage. **Always use `df -h $HOME` (or `df -h /data`)** for the real quota.
  A Termux node showing "0 free" on `/` may have tens/hundreds of GB free on
  its actual storage. (This misread caused a real fleet misjudgment: two Termux
  nodes were wrongly triaged as full when their home storage had 48G/199G free.)
- Collect per-node snapshot: % used, free bytes, timestamp, and which mount was measured
- Identify hot paths: `/tmp`, `~/.cache`, application logs, downloads, build artifacts

## Step 2 — Classify by severity

- **Critical** (<50M free on the *correct* mount): risk of OOM, update/install failures → urgent
- **Warning** (50M–500M free): constrained for new features, snapshots
- **Healthy** (>500M free): no immediate action

## Step 3 — Investigate root causes per node

- `/tmp` candidates: old temp files, build cruft, virtual env remnants
- Package caches: `~/.npm`, `~/.cache/pip`, `~/.cache/uv`, `~/.gem`
- Application logs: rotation failures or uncleared archives
- Docker/containers: `docker system df` to *measure*; `docker system prune`
  is **destructive — owner-approval gated**, and belongs to the node-local
  agent (Step 5), not the central node
- Device-specific: Termux quotas, mount restrictions, architecture constraints

## Step 4 — Create a durable ticket per constrained node

- Use the Wiki ticket flow (TM-ID; the node's Wiki-recording skill — `wiki-record` on Claude, `ccc-wiki-record` on Codex)
- Record: hostname, disk snapshot (mount measured + free bytes), root causes, suggested cleanup steps
- Mark as **opt-in** (node owner decides timing and method)
- Link to the responsible node agent

## Step 5 — Delegate to the node-local agent

- **Avoid remote rm/destructive ops from the central node** — this is policy, not preference
- Node owner/agent can assess local constraints (service state, backups, device limits)
- Node agent executes cleanup locally (safer, faster, contextual)
- Any destructive step (rm of shared paths, `docker system prune`) requires
  **explicit owner approval first**, even when executed node-locally

## Step 6 — Audit hostname / inventory accuracy

- Cross-check `hostname` against the fleet inventory
- Record any mapping errors or corrections discovered during triage
  (misattributed disk numbers between nodes are a real failure mode)
- Prevents automation confusion in future rollouts

## Step 7 — Follow-up

- After 1–2 weeks: verify tickets closed or actioned
- Update the fleet baseline (feeds capacity planning and monitoring)
- Re-audit before the next major rollout

## Safety

- Audit only; no deletion without node owner approval
- Never perform remote recursive deletes from the central node
- `docker system prune` and similar bulk-destructive commands are approval-gated
- Check for open file handles before deletion (`lsof`, `fuser`)
- Respect device-specific limits (Termux quotas, ARM constraints)
- Preserve backups for large cleanups (tar snapshot before rm)
- Do not delete active application logs or service state without verification

## Verification

- Post-cleanup: `df -h` **on the correct mount** shows >500M free on triage nodes
- Tickets closed: query the ticket system for TM-ID completion
- No unauthorized deletions: audit trail shows node-local cleanup only, no central rm scripts
- Inventory accuracy: `hostname` matches fleet inventory
- Capacity trend: track free disk over weeks to detect re-accumulation
