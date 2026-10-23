- **Erasure inventory c-batch-2: classify the harness runtime files found on
  most nodes, cover the observed backup-name variants, and keep SQLite
  sidecars with their DB (#1468, code/schema only).** A read-only fleet scan
  on 2026-10-23 found the same harness-written files on most nodes, and each
  one blocked `prune-expired`.
  - **New classes.** The files now classify through their component's own
    env chain:
    - `nunchi.gotgan_store` is the verbatim transcript store. It is
      body-bearing and handled like `mempalace.palace`.
    - `nunchi.jev_review_ledger`, `nunchi.feed_receipts` and
      `nunchi.feed_status` cover the review ledger, the feed receipts and
      the feed status files.
    - `state.skill_listing_policy` and `bot.jev_skill_advice_config` cover
      the skill-listing policy and the jev skill-advice opt-in config.
    - `skill.review_zai_env` is a live credential file and is always
      retained.
  - **Wider existing classes.** These now also cover the nunchi cron logs,
    locks and dedup state, the nunchi runtime markers, rotated
    `*.log.N[.gz]` files, and the autosave, distill, self-update and
    manifest state files.
  - **Group-b retention patterns.** These now match the observed
    `.env.bak.*`, `crontab.bak.*`, `crontab-backup-*.txt` and `+0900`-stamped
    variants, and `~/.nunchi` crontab backups. There are two new classes:
    `state.memory_facts_backups` (group b, last-copy guarded) and
    `state.cert_backups` (always retained).
  - **What stays out of group b.** The bridge's suffix-less
    `sessions.json.bak` / `tasks.json.bak` are the previous-good recovery
    copies the bridge reads when the primary is corrupt. They are now
    classified as live and never age out.
  - **SQLite sidecars.** The new `sqlite_sidecars` flag makes `-wal`/`-shm`/
    `-journal` siblings follow their DB. A sidecar of a live file is claimed
    live, and in a retention class a sidecar takes its DB's verdict.
  - **Operator one-offs.** Operator one-offs and files with no producer in
    this repo are handled by the operator-scratch class
    (`1468-operator-scratch.md`).
  - **Nothing is deleted by this change.**
