- **Turn-stall probe is now on by default at 20 minutes (#1741).**
  `CCC_TURN_STALL_PROBE_MIN` used to default to `0` (off), so the only
  automatic recovery path for a silently-dead turn was opt-in and in practice
  never enabled. Both the Telegram and Matrix frontends now read the same
  shared default (`DEFAULT_STALL_PROBE_MINUTES = 20` in `core/turn_stall.py`).
  What the probe does is unchanged: recovery still runs only on a
  confirmed-dead engine, a quiet-but-alive turn is never touched, and an
  ambiguous liveness verdict only logs. The separate notify-only turn-age
  note (`CCC_TURN_AGE_NOTIFY_MIN`, default 30, #1743) is also unchanged. To
  opt out, set `CCC_TURN_STALL_PROBE_MIN=0` in the bridge `.env` (a negative
  value also disables it; an empty or non-numeric value uses the default).
