- **Claude: between-tool progress notes are no longer dropped on Fable 5.x / Opus 5.5 (#2161).**
  Since the main sessions moved to `claude-fable-5-1`, every turn went silent:
  no interim narration reached Telegram/Matrix and the `⏳ Working` bubble was
  created once and only edited in place (never buried, never reposted at the
  room bottom). On that model generation the text written between tool calls
  comes back as a signed progress-update `thinking` block right before the
  `tool_use` it introduces (`thinking.display: "updates"`), not as a `text`
  block — a gwakga transcript showed 1 text block against 22 non-empty
  thinking blocks holding the narration verbatim — and the Claude adapter
  routed every thinking block to the private reasoning channel. Non-empty
  thinking blocks from `claude-fable-5*`, `claude-mythos-5*` and
  `claude-opus-5-5*` now take the same interim path a text block does (own
  message boundary, delivered on the next tool start); the API's
  interrupted-response sentinel is skipped and other models keep thinking
  private. `CCC_CLAUDE_PROGRESS_THINKING=false` is the kill switch;
  `CCC_CLAUDE_PROGRESS_THINKING_MODELS` overrides the prefix list.
