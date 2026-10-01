- **Retry-loop alerts repeat while an outage lasts and name its cause
  (#2086).** The Telegram `telegram_init_retry_loop` alert used to fire once
  per outage: during a 10-hour node DNS outage (7,079 failed
  `initialize()` attempts) the owner got one alert at minute one and nothing
  until recovery. A lasting outage is now re-announced 10 min and 1 h after
  it began, then every 6 h, each reminder with the elapsed time and failure
  count and its own spool dedup key (`…:stage<N>`), so a spool consumer
  never collapses it into the first alert. A slow loop that skipped several
  offsets sends one reminder, not a burst. Every alert, and the recovery
  notice (total duration + failure count), names the failure kind —
  `dns`, `tls`, `connection_refused`, `http_error`, `timeout` or `other` —
  and the deciding exception class, found by walking the
  `__cause__`/`__context__` chain (PTB `NetworkError` → `httpx.ConnectError`
  → `socket.gaierror`), never the message text. The Matrix frontend's
  `matrix_crash_loop` alert uses the same schedule and classification
  (`crash-budget.json` gains `streak_started_at`, `alert_stage`,
  `last_cause`). Schedule and classifier are pure functions in
  `utils/health_alerts.py` (`outage_realert_stage`,
  `classify_network_failure`).
