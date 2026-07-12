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
