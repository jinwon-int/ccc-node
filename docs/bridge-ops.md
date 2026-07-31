# Bridge operations

The Telegram bridge connects Telegram to Claude Code for a selected project path. It is a ccc-node app layer, separate from Hermes Gateway, A2A broker, DB/replay flows, and provider canaries.

## Start and status

- Start foreground: `bridge/start.sh --path <project>`
- Start daemon supervisor: `bridge/start.sh --path <project> -d`
- Status: `bridge/start.sh --path <project> --status`
- Stop: `bridge/start.sh --path <project> --stop`

On Linux production nodes, prefer the node's scoped `ccc-telegram-bridge.service` where configured. On Termux, avoid systemd assumptions and verify both the supervisor and `python -m telegram_bot` child.

## Safety boundaries

- Do not print bot tokens, owner chat IDs, provider keys, session files, or raw update payloads.
- Restart only the ccc bridge runtime when the change is bridge-scoped; do not restart Hermes Gateway or A2A broker as part of a bridge rollout.
- Treat Telegram/provider canaries as separate approval-gated actions.
- Codex approval requests are single-owner and turn-scoped (Allow or Deny only); never provide a session-wide Allow All.
- Never let two services poll the same Telegram bot token concurrently.

## Provider rollout

The default is `CCC_AGENT_PROVIDER=claude`. For Codex, install and authenticate
Codex CLI, set `CCC_AGENT_PROVIDER=codex` plus `CCC_CODEX_CLI_PATH` when needed,
then require `scripts/ccc-doctor.sh` to report `readiness: ready`. Stop the current
bridge before starting Codex and verify that only one poller owns the token.

Rollback is the reverse: stop Codex, restore `CCC_AGENT_PROVIDER=claude`, start
the prior Claude bridge, and again verify a single poller. Readiness checks and
source validation do not authorize a live provider/Telegram canary or restart.

## Health evidence

Useful non-secret evidence is service state, PID, restart count, `health.json` state, recent redacted warning/error classes, source commit, and test output.

For an enabled dead-session wakeup loop, `bridge/start.sh --path <project>
--status` reports cumulative, count-only scan outcomes. The `skipped` fields
cover active, locked, quarantined, cooldown, attempts-cap, and autonomous-budget
gates; a budget-only scan is therefore visible even when no wakeup is triggered.
Legacy health snapshots without these additive counters remain readable. The
`CCC_USAGE_BUDGET_TOKENS_*` settings cap only the provider's daily autonomous
input+output tokens: interactive turns remain metered in `usage-meter.json`, but
never consume that allowance or get rejected by it.

Empty normal completions (#775) are classified, not disguised as `(No response)` success: when the provider's terminal payload preserved the final answer the turn recovers it once (`requests.empty_completion_recovered` in `health.json`), otherwise the request ledger fails with cause `empty-completion` and the user gets a typed retry prompt (`requests.empty_completion_failed`). Warning logs carry the provider class name and user/chat ids only — never answer bodies.

External waits (#740): an agent's "I'll continue once CI finishes" is backed by a durable registry at `<bot_data_dir>/external-wait/waits.json` (owner-only, previous-good backup). The bridge monitor polls GitHub checks pinned to the registered exact head SHA, journals terminal transitions before waking, notifies the owning conversation, and resumes through a bridge-owned `external_event` turn (autonomous-metered). Operators inspect with `/waits` and cancel with `/cancelwait <wait_id>`; agents register via `python -m telegram_bot.core.external_wait_cli register` (see the `gh-ci-wait` skill). Kill-switches: `CCC_EXTERNAL_WAIT_ENABLED=0`, `CCC_EXTERNAL_WAIT_RESUME=0`, `CCC_EXTERNAL_WAIT_RESUME_DAILY_CAP` (default 10/day). Records and logs stay body-free — no prompts, tokens, or check logs.
