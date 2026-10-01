# Codex skill-read telemetry

The shared Codex runtime used by Telegram and Matrix records successful reads
of `skills/<name>/SKILL.md` (including `.system/<name>`) through the installed
`skill-usage-log.sh`. Records contain only `ts`, `skill`, `tool: Read`, and
`runtime: codex`; they never contain commands, file contents, paths, or chat IDs.

A read qualifies only when a live `commandExecution` start and completion share
an item ID, both carry matching structured `read` actions, the completed command
has integer exit code 0, and it returned nonempty output. Repeated completion
notifications are ignored. A separate subsequent read counts again. History
and notifications rejected by the runtime's thread/turn routing cannot count.

Capture is conservative: simple `cat`, `head`, `tail`, and `sed` commands,
including a single `bash -c`/`bash -lc` wrapper, are supported. Actual operands
must match the structured read paths. `head`/`tail` accept one file and positive
limits without verbose headers; `sed` accepts one file and a numeric print
range. Searches,
listings, mere path mentions, shell control flow, redirects, unknown actions,
missing success metadata, and arbitrary scripts or code execution are excluded.
This is evidence that a skill was loaded, not proof it was followed or useful.
Missing usage alone must not be treated as proof that a skill can be retired.

The logger resolves from `CCC_SKILL_USAGE_LOGGER`, then
`$CCC_CLAUDE_DIR/hooks/skill-usage-log.sh`, defaulting to
`$HOME/.claude/hooks/skill-usage-log.sh`. It must be an owner-owned regular file
without group/world write permission. A missing override disables capture;
it does not silently select another logger.

Owner-mode writes go to `$CCC_CLAUDE_DIR/state/skill-usage/usage.jsonl` (default
`~/.claude/state/skill-usage/usage.jsonl`), using the common logger's lock and
0600 ledger. For audience-scoped sessions, writes go under that audience's
`CCC_STATE_DIR/skill-usage`; invalid scope coordinates disable capture. Scoped
records are not combined into the owner's curator input.

Logging is best effort and never awaited on the conversation event path. Each
runtime permits at most four logger processes, with no overflow queue and a
four-second deadline. The owned process group is cleaned up after every exit,
including a successful parent exit with surviving descendants. A turn remembers at most
256 qualifying item IDs. Runtime shutdown drains the bounded pending writes.
Logger absence, overload, or failure can therefore undercount usage; none may
block a tool read. There is no historical backfill or fabricated use event.

The existing curator reads the common ledger together with autosave usage
records. Review duplicate triggers, improve descriptions, or propose retirement
only after collecting an adequate observation window and checking these capture
limits. Keep the curator's no-evidence safeguard.
