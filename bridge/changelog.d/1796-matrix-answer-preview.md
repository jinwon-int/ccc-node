- **Matrix frontend: opt-in live answer preview (#1796).** With
  `CCC_MATRIX_STREAMING=1` (default off, like `CCC_TELEGRAM_STREAMING`) the
  answer-in-progress grows in the turn's progress bubble (`m.replace` edits,
  throttled by `CCC_MATRIX_DRAFT_EDIT_INTERVAL_S`, default 2 s; tool names
  only, never arguments). It is a preview: completed intermediate messages
  still go out through the durable interim path, the bubble is cleared at the
  end, and the final answer is still delivered through the durable outbox.
  Heartbeat texts are held back while the preview shows.
