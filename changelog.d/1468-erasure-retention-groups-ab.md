- **Erasure: 30-day retention classes for legacy stores and sensitive
  backups, key files always kept (#1468 slice 3, code/schema only).** Per the
  2026-09-29 owner decision, the memory-artifact inventory now classifies
  group a (legacy unscoped `~/.nunchi` stores, stale ingest status) and group
  b (`.env.bak-*`/`.env.pre-*`, `sessions.json.bak-*`, `crontab.bak-*`) as
  age-gated `retention_policy` classes, so they no longer surface as
  unknown-artifact blockers. The planner keeps a `delete` action only for
  files older than 30 days measured from max(mtime, ctime), so `cp -p` /
  `rsync -a` copies are not instantly eligible (`retention_defaults.max_age_days`;
  `CCC_ERASURE_RETENTION_DAYS` may only lengthen it). Younger files plan as
  `retain-until:<date>`. Key files (case-insensitive key tokens such as
  `pem`, `key`, `id_ed25519`, `credential`, `secret`, `token`) are retained at
  any age, and the newest copy of a backup family is retained while its live
  file is missing. Live files are claimed by path, realpath and inode, so
  symlinked or hard-linked live files are never targets. The legacy
  `~/.nunchi` store stays claimed live regardless of `NUNCHI_*` env until the
  operator creates `~/.nunchi/.legacy-retired`, and its `facts.db` keeps
  `handoff-or-drop` on decommission. New read-only `ccc-erasure-planner.py
  retention [--json]` lists what is eligible and when (paths/dates/counts
  only). Nothing is deleted by this change: destruction still runs only
  through `ccc-erasure-apply.py` with `ERASURE_APPLY=1`, and each per-node
  run needs a separate owner approval.
