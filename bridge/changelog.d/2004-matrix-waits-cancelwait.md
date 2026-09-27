- **Matrix `/waits` and `/cancelwait` (#2004).** The Matrix frontend has
  polled external CI waits since #1934, but a room could neither list nor
  cancel them. Both commands are now owner-only (#1955) and scoped to the
  requester's own waits (legacy records without `user_id` included, as on
  Telegram); `/cancelwait` never cancels a wait registered by someone else,
  which is tighter than the Telegram form. The listing renderer moved to
  `external_wait.render_waits` and Telegram's `_render_waits` delegates to it,
  so both channels print the same text.
