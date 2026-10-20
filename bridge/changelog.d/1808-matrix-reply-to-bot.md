- **Matrix: a reply to the bot's own message now addresses it in a family
  room (#1808 slice A).** A message whose `m.in_reply_to` names an event the
  bot sent to that room is admitted without a handle, alias or wake word. The
  transport records every sent message's event id (replies, notices, files,
  the progress bubble — not `m.replace` edits) in a bounded in-memory ring of
  512 ids, oldest evicted, keyed by room. The ring is never persisted, so
  after a restart a reply to a pre-restart message needs a handle again
  (fail closed). Edits stay refused; a thread started on a bot message is
  still not run but now gets the existing thread notice instead of silence.
  Direct rooms are unchanged. HandeBot is not affected: its native bot does
  the room addressing before a turn reaches the bridge.
