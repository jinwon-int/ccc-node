- **The Matrix `⏳ Waiting for results` status moves back to the room bottom
  when buried (#2288).** The per-conversation external-wait status (#2088) was
  only ever edited in place, and an `m.replace` keeps the original timeline
  position, so after the owner's next message and the reply it stayed buried
  for good. It now follows the `⏳ Working` bubble's rule: while it is still the
  room's newest event (or its own latest edit is), changes are edits; once
  newer events have landed, the next sync redacts it and posts the current text
  again through the durable outbox (after the reply row, so it lands last) —
  while a wait is still being watched; a line that only shows finished waits
  is edited in place and expires as before (the resume turn's reply already
  announced the result).
  Planner (`plan_status_update`) gains a `move` action behind a `buried` flag
  that only the Matrix projection sets — Telegram keeps editing in place. The
  store entry carries two optional facts, `tail` (latest edit event id, from
  the new `edit_notice_event`) and `posted_epoch`; two guards stop a repost
  loop: the outbox `delivered` hook never moves (the fresh row's `/sync` echo
  is not in yet) and a status posted under `MOVE_MIN_INTERVAL_S` (10 s) ago is
  left alone. Still an `m.notice`, so clients do not notify on the repost.
