- **fix(hooks): bound `session-archive` growth — content-addressed SessionEnd snapshots + keep-newest retention (#2361).**
  `notify.sh` copied `working-state.md` into `~/.claude/state/session-archive/`
  on every SessionEnd under a name derived from the hook event, so the same
  unchanged working state was stored once per session (one Claude node held
  416k files / 18 GB, 115 of 400 sampled files byte-identical). The snapshot
  name is now `sha256("session_end" NUL content)` (new
  `ccc_lifecycle_file_digest`, shared with the bridge archive): an identical
  snapshot is only re-stamped (mtime) and a differing body under that name is
  atomically replaced. After each write the newest
  `CCC_SESSION_ARCHIVE_KEEP` snapshots are kept (default 200, `0` disables
  pruning) via the existing portable `prune_keep_newest`. Existing archives are
  pruned to the cap on the next SessionEnd. Checkpoints are unchanged.
