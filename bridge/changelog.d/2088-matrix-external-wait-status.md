- **External-wait status message on Matrix (#2088, stage 2 of #2081).** The
  Matrix frontend now keeps the same ONE per-conversation `⏳ Waiting for
  results · PR #… CI` status as Telegram, on the shared renderer, planner,
  flag (`CCC_EXTERNAL_WAIT_STATUS`) and `status-messages.json` store. The
  first message is a durable outbox notice queued after the turn's reply row,
  so it always lands below the reply with the outbox's pinning, room gate and
  retry; the sender records the Matrix event id a delivered row became (new
  idempotent `jobs.sent_event` column, written in the same transaction as the
  part acknowledgement) and a `delivered` runner hook adopts it. Later
  changes are `m.replace` edits of that event, the clean-up is a redaction,
  and a refused edit re-posts only while a wait is still monitored.
  Triggers: a new transport `turn_closed` runner hook (every turn, including
  external-wait resume and continuation self-jobs), every terminal transition
  in the monitor (`status_syncer`), Matrix `/cancelwait`, and a bounded
  background reconcile after start. Syncs are serialized per bot.
  Registered as side-effect operation `matrix.external_wait_status`.
- **Telegram: the remaining turn paths refresh the wait status (#2088).** The
  skills prompt, `/task_resume`, the slash `run_task` closures (`/command`,
  skill slash commands) and the numbered-option callback now refresh the
  route's status after their reply, like the main message path (stage-1 gap).
