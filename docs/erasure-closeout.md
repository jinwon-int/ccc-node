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

Armed runs hold a shared lock in the backup base and revalidate the plan
under that lock. Every run gets its own exclusive backup directory, including
two runs in the same second. Before the first deletion, `manifest.json` is
durably written with `phase: prepared` and the target-to-backup mapping. The
final atomic update sets `phase: completed` and records deletion failures and
verification. If a run is interrupted, a prepared manifest still identifies
the recovery files; it does not claim that all targets remain present.

The planner preserves each inventory entry's explicit action for
`cache-rebuild`, `prune-expired`, and `telegram-user-erasure`; unsupported
actions remain visible in the plan and are skipped by apply. Audience
erasure remains restricted to its audience root. Primary paths are not repeated
as secondary targets, and conflicting actions on a present path block apply
before deletion.

## Retention classes — groups a/b (#1468)

Owner decision (2026-09-29, option ②): group a (legacy unscoped stores,
e.g. `~/.nunchi/{facts.db,backend-health.json,snapshot.md}` once the live
resolver points elsewhere) and group b (sensitive backups: `.env.bak-*`,
`.env.pre-*`, `sessions.json.bak-*`, `crontab.bak-*`) are kept **30 days**,
then become **eligible** for destruction at the apply boundary. Key files are
always kept.

- Inventory: entries with a `retention_policy` object (`group`, optional
  `max_age_days`); defaults live in `retention_defaults` (`max_age_days: 30`,
  `age_source: max(mtime,ctime)`, `key_file_patterns`). Their resolve
  candidates are anchored name patterns only.
- Age is measured from the **later of mtime and ctime** (never contents).
  `cp -p`, `cp -a`, `rsync -a` and `shutil.copy2` carry the source's old
  mtime, so a backup taken today must not look months old; ctime cannot be
  backdated. A chmod/rename/restore also refreshes ctime and so restarts the
  clock (errs toward keeping).
- `CCC_ERASURE_RETENTION_DAYS` may only **lengthen** retention; shortening it
  is a reviewed inventory change.
- Key files plan as `retain (key-file)` at any age. The rule searches the
  file name case-insensitively for key tokens on `.`/`_`/`-` boundaries —
  `pem`, `p12`, `pfx`, `jks`, `keystore`, `key(s)`, `gpg`, `asc`, `age`,
  `id_rsa`/`id_dsa`/`id_ecdsa`/`id_ed25519`, `credential(s)`, `secret(s)`,
  `token(s)`, `oauth`, `auth.json`, `netrc`, `hosts.yml` — so
  `.env.bak-x.PEM` or `.env.bak-ID_ED25519` are kept. The inventory can add
  patterns, never remove the built-in ones. Key backups
  (`memory-audience.key.bak-*`) also carry an explicit `retain` action.
- Live files are never retention targets. A path any non-retention class
  resolves as live is claimed by **path, realpath and inode**, so a live
  `.env` that is a symlink to `.env.pre-mig`, a `NUNCHI_DB` symlinked onto
  `~/.nunchi/facts.db`, or a hard link of a live file all stay protected.
- Legacy `~/.nunchi/{facts.db,snapshot.md,backend-health.json}` is still read
  through `CCC_MEMORY_LEGACY_NUNCHI_HOME` even while `NUNCHI_DB` /
  `NUNCHI_SNAPSHOT` point at the audience store, and reads never bump mtime.
  So these stay claimed live **regardless of env** until the operator creates
  the retirement marker `~/.nunchi/.legacy-retired` (default absent; itself
  classified as retained). Only then does their 30-day clock matter. For
  `facts.db`, `node-decommission` stays `handoff-or-drop` (never a plain
  delete), so decommission keeps its handoff contract.
- Last copy: while a backup family's live counterpart is absent (`.env` for
  `.env.bak-*`/`.env.pre-*`, `sessions.json` for its backups; crontab has no
  checkable file, so it always counts as absent), the newest copy per
  directory is planned as `retain (last copy; live missing)` even past
  retention.
- Dry-run: `scripts/ccc-erasure-planner.py retention [--json]` lists every
  retention file with its group, mtime, age basis, `eligible`, `eligible_at`
  and planned action — paths, dates and counts only. `prune-expired` /
  `node-decommission` plans carry `delete` only for expired files; younger
  ones plan as `retain-until:<ISO date>`, which apply skips.

**Out of scope here:** actually deleting anything on a node. Eligible files
are destroyed only by a `prune-expired` plan run through
`ccc-erasure-apply.py` with `ERASURE_APPLY=1` (digest, blockers, owner-only,
rollback-first), and every such per-node run needs its **own fresh owner
approval**. Measuring the two Termux nodes is also still
open (#1468).

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
