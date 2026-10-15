- **fix(memory): Codex distill snapshots read the Codex CLI 0.149+/0.160 item
  format, and a thread with no speech is no longer an empty success (#2316 C).**
  0.160.1 rollouts record no `event_msg/user_message` at all: speech lives in
  `event_msg/item_completed` turn items (`UserMessage`/`AgentMessage` with a
  `content` part list), so the rollout reader returned zero messages and four
  live jobs ended `extraction_done` with `snapshot_bytes=0`. The rollout reader
  now maps both the legacy events and the `item_completed` items (only
  `event_msg` records; `response_item` messages stay ignored), and drops a
  legacy event when an unmatched turn item of the same role carries the same
  text, so one utterance is never counted twice while legacy-only speech is
  kept. `thread/read` mapping (verified against the 0.160.1 app-server v2
  schema: `userMessage{content: UserInput[]}` / `agentMessage{text}`) also
  accepts the PascalCase core spelling and an agent `content` list. Only speech
  counts against `TranscriptBounds.max_items` on both paths, so a tool-heavy
  tail no longer exhausts the budget before any message; the RPC walk is capped
  at 20,000 raw items, and the rollout reader widens once to its 16 MiB byte
  bound when the default tail window holds no speech. A readable thread that
  yields no speech now falls back to the rollout file and otherwise raises the
  retryable `SnapshotUnavailableError`, per the `distill_types` contract.
