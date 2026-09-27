- **Matrix transport robustness bundle (#1959).** Retryable homeserver
  statuses now raise `MatrixTemporaryError` carrying the server's requested
  wait (`Retry-After` / 429 `retry_after_ms`, capped at 300 s), and the retry
  loop honours it; a leg that ran ≥ 60 s before failing restarts its backoff
  at 1 s instead of staying at 30 s. `/stop` bounds the runner's graceful
  cancel (10 s) because it runs under `matrix_lock`, so a hung provider no
  longer freezes sync and sending. A 404 on the room's `m.room.encryption`
  state is classified `encrypted-room-required`. A push-spool record that is
  valid JSON but not an object is archived instead of jamming the spool head
  (Matrix and Telegram notifiers). `matrix-ids.json` and
  `matrix-direct-rooms.json` writes fsync the file and directory. The default
  Matrix frontend now withholds nio/aiohttp log bodies like the Grok ones.
  `/skills` uses the shared dispatch path (still provider defaults, fresh
  session). `telegram_bot.core.matrix` is listed for the wheel. Inbox DB
  pruning is not part of this change.
