- **External-wait status message (#2081, stage 1 — Telegram).** When a turn
  ends while the conversation still has registered CI waits, the bridge posts
  ONE silent status line per conversation (`⏳ Waiting for results · PR #598
  CI — <next step> (registered 12:34, up to 6h)`, one line per wait,
  chronological, summary credential-redacted) and then edits that same
  message as waits finish (`✅ CI green → continuing`, `❌ CI failed →
  investigating`, `⚠️ CI cancelled`, `🔀 head moved`, `⏰ expired`,
  `🚫 cancelled by owner`). Once no wait is being monitored the message shows
  the outcomes from the last 30 minutes, or is deleted when there are none.
  Exactly one message per route; nothing is sent for a route with no waits.
  Edit/delete failures (`message is not modified`, `message to edit not
  found`, …) are swallowed fail-open and a vanished message is re-sent only
  while something is still watched. Triggers: after the reply on the normal
  user-message path (text, voice, queued follow-ups), after an external-wait
  resume or continuation turn's reply, on every terminal transition inside
  the monitor (new optional `ExternalWaitMonitor(status_syncer=…)`), and
  after `/cancelwait`. A bridge restart reconciles stored messages (refresh,
  finalize, or delete). New `CCC_EXTERNAL_WAIT_STATUS` flag (default on);
  store at `.telegram_bot/external-wait/status-messages.json`; renderer and
  store are channel-neutral in `core/external_wait_status.py` so the Matrix
  frontend can reuse them once its outbox returns event ids (follow-up).
  Registered as side-effect operation `telegram.external_wait_status`.
