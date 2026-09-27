- **Matrix long-reply test waits for the recorded turn (#1976).**
  `test_long_reply_is_delivered_whole_as_budgeted_parts` waited for "`raw()`
  awaited and outbox empty", but `input()` spawns an early typing PUT through
  the same `raw()`, so on a loaded runner the wait could end before the turn
  finished and `last_turn` was still unset (`TypeError: 'NoneType' object is
  not subscriptable`, bridge-tests 3.11). The wait now also requires
  `last_turn`, which `run_turn` writes only after `finish()` queued the reply,
  so the empty outbox means the reply was delivered. Bounded at 5 s; every
  assertion is unchanged. Test-only change.
