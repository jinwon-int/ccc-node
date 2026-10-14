- **`start.sh --channel matrix --status` now shows the Matrix frontend's
  health instead of the Telegram bridge's (#2312).** `--status` always rendered
  `<path>/.telegram_bot/health.json` with the Telegram pid file, whatever the
  requested or inherited channel, so an operator asking whether the Matrix
  frontend was idle saw the Telegram bridge's `Turn occupancy: idle` while a
  Matrix turn was running. For the Matrix channel `--status` now finds the live
  frontend for the path (the same channel-aware match `--stop` uses), reads
  `BOT_DATA_DIR` and `CCC_AGENT_PROVIDER` from that process's environment
  (a relative data dir resolves against the frontend's working directory),
  prints the health path it used and renders that file with a `Matrix`
  transport line. It never falls back to the Telegram file: no frontend reports
  `unavailable`, several frontends or an unreadable data dir report `degraded`.
  The Telegram `--status` output is unchanged. `utils/health_render.py` takes
  an optional fifth `transport_label` argument (default `Telegram`).
