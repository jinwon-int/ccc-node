- **Skill autosave: re-running `install-skill-autosave-cron.sh` no longer
  silently drops the baked provider/drafting lane (#1867).** A flagless
  re-run used to re-render the managed cron block from scratch, so nosuk lost
  `CCC_SKILL_PROVIDER="piri"` and `CCC_SKILL_PIRI_DRAFTING=1` and its piri
  drafting skipped as `not-enabled` for nine days. The installer now reads the
  existing managed entry and carries forward `CCC_SKILL_PROVIDER`,
  `CCC_SKILL_{PIRI,CODEX,DANSO}_DRAFTING`, `CCC_DANSO_STATE_DIR` and
  `CCC_SKILL_PROMOTION_PROVIDERS` unless an explicit flag (or inherited env)
  sets them, reports what it kept on stderr (`NOTICE: kept lane settings …`),
  and materializes the kept values into the install record argv. An unsafe
  baked state dir is never re-baked. New `--reset-lane` drops the baked lane
  on purpose. Every `--apply` appends a row (ts, action, gen, invoked argv,
  rendered argv, preserved keys) to the owner-only
  `<state>/skill-autosave-cron.history.jsonl`, so a later loss can be dated.
- **Skill autosave: an absent non-Claude drafting opt-in with recent sessions
  is now a visible signal (#1867).** When the codex/piri/danso lane is not
  enabled and no explicit `CCC_SKILL_<LANE>_DRAFTING` value is set, but the
  lane's session tree has a session within the sweep window (first-hit
  `find -quit` probe; the tree is still not walked), the skip line gains
  `recent_sessions=yes` and the sweep logs
  `WARN lanes-not-enabled lanes=<lanes>`. Set `CCC_SKILL_<LANE>_DRAFTING=0` to
  mark a deliberate opt-out.
