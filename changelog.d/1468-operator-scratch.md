- **Erasure planner: operator one-off files in the harness state dir become
  the `operator-scratch` retention class (#1468, owner decision 2026-10-23).**
  They are kept 90 days, then become eligible at the existing apply boundary.
  - **Fallback, not a pattern.** The new `state.operator_scratch` entry
    carries a `fallback_scope` (state dir via `CCC_STATE_DIR`, then
    `~/.claude/state`). A file directly under that root that matches no other
    class becomes scratch instead of a blocker. Files that another class
    matches keep that class. Subdirectories are never entered.
  - **Scheduler guard.** A scratch file named by the crontab
    (`CCC_CRONTAB_CMD`), the agent-cron task store or a systemd unit file is
    retained at any age. If any of those sources is unreadable, no scratch
    file is eligible, and the plan says why in a body-free `scheduler_guard`
    block.
  - **Rule order.** Live claims, key files, symlinks and `max(mtime, ctime)`
    age all apply as before. `CCC_ERASURE_RETENTION_DAYS` still only
    lengthens. Decommission gates scratch the same way it gates group b.
    Audience, user and cache requests never target scratch.
  - **Former owner-decision backups.** `wiki-candidates.md.bak*`,
    `working-state.md.bak*` and `skill-autosave.mode.bak-auto-*` are now
    scratch.
  - **Scope.** `~/.nunchi` and the bot data dir hold private memory and
    credentials, so they get no fallback and unknown files there still block.
  - **New class.** `state.timed_test_notify_state` classifies the
    timed-test scan's notify dedup state.
  - **Nothing is deleted by this change.**
