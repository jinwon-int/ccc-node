- **Memory: a distill job is never snapshotted by another provider's reader,
  and a missing Claude transcript no longer counts as an empty conversation
  (#2316 A/B).** The snapshot worker read every journal job with the bridge's
  current runtime, so after a codex->claude switch the departing Codex
  session's `provider_switch` job (a UUID thread id that also passes the
  Claude session-id check) was read by the Claude reader, which returned an
  empty snapshot: the job completed with no memory and nothing reported it.
  The worker now routes each job by its own provider. A Claude job left by a
  claude->codex/piri switch is read from the same served transcripts directory
  `ClaudeRuntime` uses; a Codex/Piri/Danso job with no reader in this bridge
  stays retryable as `snapshot_provider_mismatch` and ends as
  `max_attempts_exceeded` (visible in the journal) instead of a terminal
  `invalid_snapshot_request` or a silent empty success. `read_claude_snapshot`
  now raises `SnapshotUnavailableError` for a missing transcript file, like
  the Codex/Danso/Piri readers.
  The retry window is the snapshot worker's 5 attempts times
  `distill_extraction_poll_interval` (default 300s, about 25 minutes): a
  job left behind by a provider switch is read only if the bridge switches
  back within it; otherwise the loss is now visible instead of silent.
