- **nunchi: audience-scoped collection works on Claude-provider nodes (#1921).**
  `install-nunchi.sh` refused `--claude --audience-scoped` ("currently requires
  Piri"), so an audience-scoped Claude bridge had no re-apply path and its
  collection sat at 0 while the old cron kept ticking. The bridge now writes,
  after every successful Claude turn (Telegram and Matrix), a body-free
  owner-only sidecar `<root>/<scope>/claude/session-map/<session_id>.json`
  mapping the Claude session id to the audience `resolve_memory_audience`
  resolved (atomic rename, 0700/0600, no message content). In scoped mode
  `ingest-cron.sh` hands off to the new `claude-audience-feed.py`, which routes
  each bridge distill-journal job (and each scope's own
  `<scope>/state/distill-history`, into that scope only) into exactly one
  `<scope>/nunchi` store; unmapped, ambiguous or invalid mappings are skipped
  fail-closed, retried on later ticks and counted in `ingest.status.json`. The
  node-wide `~/.claude/state/distill-history` (non-bridge CLI/cron/worker
  sessions) is never read in this mode, so a terminal `claude --resume` of a
  room session cannot route private facts into the shared store.
  External-wait resume and continuation turns record their route too, so a
  session reused across surfaces is `ambiguous`. Sidecars older than
  `CCC_NUNCHI_CLAUDE_SIDECAR_MAX_AGE_DAYS` (default 90) with no pending input
  are pruned, and the cron log only speaks when the counts change. The
  installer accepts the combination (no verbatim MemPalace sweep for it — that
  sweep cannot route per session) and keeps refusing Codex/Danso. Until the
  audience root holds any sidecar, the tick reports
  `skipped: no-audience-sidecar` and `ccc-doctor` adds a
  `nunchi claude audience map` DEFECT row (also when no sidecar parses as a
  valid record, or the cron root differs from the bridge's audience root); it
  also flags a Claude runtime left on another provider's scoped lane.
