- **Erasure: 30-day retention classes for legacy stores and sensitive
  backups, key files always kept (#1468 slice 3, code/schema only).** Per the
  2026-09-29 owner decision, the memory-artifact inventory now classifies
  group a (legacy unscoped `~/.nunchi` stores, stale ingest status) and group
  b (`.env.bak-*`/`.env.pre-*`, `sessions.json.bak-*`, `crontab.bak-*`) as
  age-gated `retention_policy` classes, so they no longer surface as
  unknown-artifact blockers. The planner keeps a `delete` action only for
  files whose mtime is older than 30 days (`retention_defaults.max_age_days`;
  `CCC_ERASURE_RETENTION_DAYS` may only lengthen it); younger files plan as
  `retain-until:<date>`, key files (`*.key*`, `*.pem*`, private-key and
  credential names) plan as retained at any age, and a path a live class
  still resolves is never a target. New read-only `ccc-erasure-planner.py
  retention [--json]` lists what is eligible and when (paths/dates/counts
  only). Nothing is deleted by this change: destruction still runs only
  through `ccc-erasure-apply.py` with `ERASURE_APPLY=1`, and each per-node
  run needs a separate owner approval.
