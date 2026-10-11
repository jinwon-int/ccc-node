- **Task heartbeat default 10s → 5s (#2233).** The "⏳ Working — …" status
  bubble now appears 5s after a turn starts and refreshes every 5s
  (`CCC_HEARTBEAT_THRESHOLD_SECONDS` / `CCC_HEARTBEAT_UPDATE_INTERVAL_SECONDS`
  defaults; the `project_chat` fallbacks and the Matrix `STATUS_MIN_INTERVAL_S`
  throttle follow). The turn-loop tick (`TYPING_INTERVAL`, shared by typing
  refresh and the heartbeat check) drops 4s → 2.5s so a 5s interval lands on a
  5s tick instead of the next 8s one; typing refresh is now every 2.5s (under
  Telegram's ~5s expiry). Per-node env overrides keep working.
