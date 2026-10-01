# Danso skill usage

The bridge records successful native `read` calls through the same
`skill-usage-log.sh` used by Claude, Piri and Codex. Rows contain only `ts`,
`skill`, `tool: Read`, and `runtime: danso`. Counts measure loads, not usefulness.

Capture requires `CCC_DANSO_PROGRESS_ENABLED=true` (the default) and a native
binary exposing `--progress-jsonl`. Each new process observes only its live
stream, including a task continuation. It does not scan stored journals.

The adapter correlates an assistant tool call, a sequential tool start, a
matching `toolResult` with `isError: false` and nonempty text, and a successful
settlement. Only exact `*/skills/<name>/SKILL.md` paths (including `.system`
skills) qualify. Relative paths use the turn workspace. Traversal paths,
duplicate call IDs/results, failed or incomplete reads, empty range results,
path mentions and other tools do not count. Ranged reads must contain file
content after the native range header. Correlation is bounded to 64 calls per
batch and 256 per process; ambiguous or overflowing streams disable capture
for the remainder of that process without interrupting the conversation.

The private observer retains only bounded call IDs, tool names, skill names
and result flags. Paths, arguments and tool bodies remain absent from normalized
bridge events and ledger rows. The logger receives a synthetic skill path.

The bridge resolves the logger from the host `CLAUDE_SETTINGS_PATH` directory
(or explicit `CCC_SKILL_USAGE_LOGGER`), separately from Danso's private `HOME`.
Logger variables and host credentials are not added to the native child
environment. Audience-scoped sessions route to their validated
`CCC_STATE_DIR/skill-usage`; invalid scope never falls back to the owner ledger.
Scoped rows remain outside the owner curator's input.

Codex and Danso share the bounded logger implementation: at most four children
per runtime, a four-second child timeout, no waiting queue, and process-group
cleanup. Turn cleanup drains outstanding writes. Missing or unsafe loggers and
write failures leave the conversation usable; this is best-effort telemetry.

Verification must distinguish installation, serving code, configuration and an
actual read. Use isolated usage state for canaries so operational counts are
not inflated. For nodes with separate Telegram and Matrix Danso state roots,
verify skills under each `<state>/home/.pi/agent/skills` directory.
