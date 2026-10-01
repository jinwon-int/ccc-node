- **External-wait resume and continuation turns no longer reuse the sender's
  DM session inside a group (#2075).** Both bridge-started runners looked up
  `get_session(user_id)` — the sender's DM row — and then ran the turn in the
  record's group `chat_id`, so DM context could continue inside a family room
  (and a valid group wait was often skipped as `session_moved`, because the
  guard compared it with the DM session). The Telegram resume, its
  session-moved guard and the continuation runner now resolve the session
  through the same `_conversation_key(user_id, chat_id)` an ordinary turn
  uses, so they honour `CCC_TELEGRAM_SESSION_SCOPE` (`per-user-chat`,
  `shared-groups`, `shared-all`) exactly like a user message. DM records keep
  resolving the DM row. Matrix already used the conversation key and is
  unchanged.
