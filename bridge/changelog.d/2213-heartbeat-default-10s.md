- **Task heartbeat default 15s → 10s (#2213).** The "⏳ Working — …" status
  bubble now appears 10s after a turn starts and refreshes at least every 10s
  (`CCC_HEARTBEAT_THRESHOLD_SECONDS` / `CCC_HEARTBEAT_UPDATE_INTERVAL_SECONDS`
  defaults; the `project_chat` fallbacks and the Matrix `STATUS_MIN_INTERVAL_S`
  throttle follow). Per-node env overrides keep working. The refresh loop still
  ticks every 4s (typing refresh), so the observed cadence is 10–14s.
