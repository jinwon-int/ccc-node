# Erasure closeout checklist (#873 step 5)

The ordered workflow that connects a lifecycle request to external owners
(Family Wiki, operator) and — only at the very end — to the approval-gated
apply boundary (`scripts/ccc-erasure-apply.py`). Design: issue #873, step-5
comment (2026-09-04). The node never edits or deletes Wiki/operator material;
the handoff manifest is a REQUEST, not an action.

## Order

1. **Plan (read-only)** — `scripts/ccc-erasure-planner.py <request> --json`.
   Blockers must be classified before anything downstream runs.
2. **Handoff manifest** — `scripts/ccc-erasure-handoff.py <request> [--queue
   <wiki-candidates.md>]`. Writes the versioned manifest + a human-readable
   twin into `$CCC_STATE_DIR`. The manifest records the plan digest (the
   closeout START state), external owners, operator decision rows, Wiki
   disposition proposals, outbox backlog, and an empty owner-ACK row.
3. **Drain first** — every outbox-role class with a pending backlog
   (`drain_first` section) is reviewed/pruned BEFORE the apply step:
   wiki-candidates entries get their human review (promote via `/wiki-record`
   or reject), drained journal entries get pruned. Draining changes the
   world — the apply step re-plans fresh and binds a NEW digest, which is
   correct; the manifest documents where the closeout started.
4. **Wiki disposition** — per family-wiki artifact, the owner picks one:
   - `annotate` (default) — a deprecation/freeze banner via a normal
     wiki-record PR; content is never deleted.
   - `archive` — move under `pages/archive/` per Wiki convention.
   - `retain` — node facts stay as canonical history.
   Merge refs are recorded in the manifest (`.decision` fields / PR links).
5. **Owner ACK** — a Fresh-approval manual ACK. The manifest `ack` row is
   filled (`granted_at`, `granted_by`). An apply run must treat a
   non-granted ACK as a blocker.
6. **Apply (approval-gated)** — `scripts/ccc-erasure-apply.py` re-plans
   fresh, binds its own digest, and runs the 4-condition boundary
   (digest / blockers / owner-only / rollback-first). See the apply module
   docstring for the full contract.

## Wiki promotion records (#1447 batch)

The nunchi wiki-promote batch embeds `<!-- nunchi-p3-8 fact#ID -->` markers
in every queue entry it writes. At closeout, pass the queue to the manifest
writer (`--queue`) so promotion records travel with the handoff: statuses
(pending/merged/rejected) and the fact ids, so the owner can cross-reference
promoted TM pages. `install-nunchi.sh --remove` stops the batch cron first;
the seen ledger is a deletable dedup artifact (benign loss).

## Artifacts

| artifact | written by | notes |
|---|---|---|
| `erasure-handoff-<request>-<stamp>.json` / `.md` | `ccc-erasure-handoff.py` | classified in the inventory (`erasure.handoff_manifests`); retain/archive at owner ACK |
| `manifest.json` in the apply backup dir | `ccc-erasure-apply.py` | mutation record of the final apply run (#873 step 4) |
