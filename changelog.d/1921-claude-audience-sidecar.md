- **nunchi: audience-scoped collection works on Claude-provider nodes (#1921).**
  `install-nunchi.sh` refused `--claude --audience-scoped` ("currently requires
  Piri"), so an audience-scoped Claude bridge had no re-apply path and its
  collection sat at 0 while the old cron kept ticking. The bridge now writes,
  after every successful Claude turn (Telegram and Matrix), a body-free
  owner-only sidecar `<root>/<scope>/claude/session-map/<session_id>.json`
  mapping the Claude session id to the audience `resolve_memory_audience`
  resolved (atomic rename, 0700/0600, no message content). In scoped mode
  `ingest-cron.sh` hands off to the new `claude-audience-feed.py`, which routes
  each distill-history snapshot and bridge distill-journal job into exactly one
  `<scope>/nunchi` store; unmapped, ambiguous or invalid mappings are skipped
  fail-closed, retried on later ticks and counted in `ingest.status.json`. The
  installer accepts the combination (no verbatim MemPalace sweep for it — that
  sweep cannot route per session) and keeps refusing Codex/Danso. Until the
  audience root holds any sidecar, the tick reports
  `skipped: no-audience-sidecar` and `ccc-doctor` adds a
  `nunchi claude audience map` DEFECT row; it also flags a Claude runtime left
  on another provider's scoped lane.
