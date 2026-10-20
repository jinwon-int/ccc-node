- **Matrix: follow-ups in a family room need no handle for a while (#1808
  slice B).** After the bot completes a turn for a family member's message,
  that member's next messages in the same room are addressed for
  `conversation_hold_minutes` (Matrix config, default 10, `0` = off, values
  above the 120-minute hard cap are clamped and logged, invalid values stop
  the service). The hold is per sender per room: other members' chatter is
  never admitted by it and never extends it, while each of the sender's own
  completed turns (held follow-ups included) restarts the window. Messages
  whose `m.mentions` name other users but not the bot, and replies to events
  the bot did not send, are not admitted by the hold. Edits, threads and the
  "not read" notices are unchanged. The window stays within
  `AUTO_NEW_SESSION_AFTER_HOURS` (clamped and logged when larger, and each
  hold ends at the opening turn's start + the reset) so a held answer never
  lands in a session about to be reset. The table is in memory only, so a
  restart clears every hold (fail closed). Cancelled, timed-out and uncertain
  turns and agent self-jobs open no hold. Direct rooms are unchanged.
  HandeBot is not affected: its native bot does the room addressing.
