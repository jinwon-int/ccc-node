- **The Matrix external-wait status line is an `m.notice` (#2088).** The
  per-conversation `⏳ Waiting for results` message went out as `m.text`, so
  clients could ping or notify on a bot status line. The first send (a durable
  outbox row, marked by `jobs.body = "notice:m.notice"` on a `$notice-` row,
  including the plain-text 413 retry), its `m.replace` edits (`edit_notice`)
  and their `m.new_content` are now all `m.notice`. Assistant replies and every
  other bot notice stay `m.text`. Inbound: the bot's own `m.notice` events now
  count as trusted reply parents (`_trusted_text`), so a reply to the status
  message is quoted with its text after a cache miss or restart, and a
  successful `edit_notice` refreshes the reply cache so a reply quotes the
  current status, not the first one. The bot's own notices and their edits
  are still never admitted, refused or counted as unsupported kinds.
