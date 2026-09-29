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

## Before a manual restart: occupancy check

A restart SIGTERMs the bridge, which closes admission and drains for at most
45 seconds (`_SHUTDOWN_DRAIN_SECONDS`) before tearing down whatever is still
running. An in-flight turn or tracked background job is then killed and the
user's work is lost. Before any manual `systemctl restart`, `start.sh --stop`,
or checkout swap, read the bridge's own occupancy record (#1881).

The canonical source is `workload` in `<project>/.telegram_bot/health.json`,
normally `~/.telegram_bot/health.json`. The same file is the default for the
self-update idle gate. The Matrix frontend writes the same shape to
`~/.ccc-matrix/health.json`. The occupancy fields are:

| Field | Meaning |
|---|---|
| `workload.turn_occupancy.state` | `idle` or `occupied`. `occupied` exactly when `active_requests > 0`. |
| `workload.active_requests` | Tracked in-flight work: the larger of provider sessions (including provider-owned background tasks such as Claude run-in-background Bash) and accepted Telegram run tasks. |
| `workload.oldest_request_age_seconds` | Age in whole seconds of the oldest tracked item. `0` when idle. |
| `workload.waiting_for_turn` | Requests queued for runtime admission, never more than `active_requests`. |
| `workload.turn_occupancy.observed_at` | UTC time of the observation. It is missing only before the first reporter tick. |
| `workload.turn_occupancy.oldest_turn_started_at` | Present only while `occupied`. `occupied_since` is a legacy alias and `elapsed_seconds` repeats the age. |

Copy-paste check (jq):

```bash
jq -r '.workload as $w | "\($w.turn_occupancy.state) active=\($w.active_requests) oldest=\($w.oldest_request_age_seconds)s waiting=\($w.waiting_for_turn) observed_at=\($w.turn_occupancy.observed_at) updated_at=\(.updated_at)"' ~/.telegram_bot/health.json
```

For scripts, `jq -e '.workload.turn_occupancy.state == "idle"' ~/.telegram_bot/health.json`
exits `0` only when the bridge reports idle. A missing field makes jq print
`false` or `null` and exit non-zero, so the check fails closed. Without jq:
`python3 -c 'import json,os;w=json.load(open(os.path.expanduser("~/.telegram_bot/health.json")))["workload"];print(w["turn_occupancy"]["state"],w["active_requests"],w["oldest_request_age_seconds"])'`.
`bridge/start.sh --path <project> --status` shows the same data in its
`Turn occupancy` line and reports `unknown` when the observation is stale.

How to read the result:

- **`idle`, and `updated_at` is recent**: safe to restart. While the bridge
  is idle the workload record is rewritten about every 30 seconds, and the
  reporter samples every 10 seconds. The self-update gate treats a snapshot as
  fresh for 90 seconds.
- **`occupied`**: do not restart. Wait, or ask the owner. Long-running work can
  keep a bridge `occupied` for a long time, such as a provider background job
  or an accepted run task. In the 2026-09-21 case the tracked item was a paused
  long task. That is still tracked work, and the drain gives up after 45
  seconds. For Danso, pause the task before restarting so the restart does not
  spend an interrupted-request slot (see [danso-telegram.md](danso-telegram.md)).
- **Stale, unreadable, or missing fields**: treat as *unknown*, never as idle.
  The self-update gate fails open here, but a manual restart should not. Use
  `--status`, or confirm with the owner.

> **Warning:** `health.json` has **no** `active_turns` field, and no top-level
> or `workload.active` field. A check that reads them gets `null`/`0` and
> reports "idle" while the bridge is busy. On 2026-09-21 this let two nodes be
> restarted with tracked work still running (#1881). Read
> `workload.turn_occupancy.state` and `workload.active_requests` only.

The fields are written by `RuntimeHealthReporter.record_workload`
(`bridge/utils/health.py`). The counts come from
`_bridge_workload_snapshot` (`bridge/core/bot_lifecycle.py`); the Matrix
frontend has its own `_workload_snapshot`. The automated consumer that
applies this gate is `health_file_busy` in `scripts/ccc-self-update.sh`, which
defers with exit `8` (see [self-update.md](self-update.md#idle-gate-dont-restart-mid-task)).

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
`CCC_USAGE_BUDGET_TOKENS_*` settings (fleet default 2,000,000 per provider per
KST day since 2026-09-02; `0` disables) cap only the provider's daily autonomous
input+output tokens: interactive turns remain metered in `usage-meter.json`, but
never consume that allowance or get rejected by it. Piri is the exception since
2026-09-18: its fleet default is `0` (request-count metering saturated the shared
default), so piri autonomous spend is metered but uncapped unless a node sets a
finite value.

Codex long-thread visibility is intentionally bounded. The resume path keeps
using `excludeTurns` plus a one-turn `thread/turns/list` check when supported;
an older app-server is reported as `compatibility_fallback` after the existing
full-resume fallback. `health.json → codex_resume` and `--status` expose that
mode for the latest process observation across its audience runtimes (not
the current chat), an explicitly named `last-turn` item count (never a total thread count),
and `observed_result_json_bytes` as unknown: no transport size scalar is
available, and even one turn can contain large tool bodies, so diagnostics
do not serialize them to compute a byte count. Missing observations remain
unknown/not observed. Compaction does not shrink the full resume frame, and
rollout file size is not a resume-cost threshold; operators should not expect this
diagnostic to compact, rotate, reset, or scan a thread. Provider deployment,
compatibility rollout, and any future operational threshold remain explicit
follow-up decisions.

Empty normal completions (#775) are classified, not disguised as `(No response)` success: when the provider's terminal payload preserved the final answer the turn recovers it once (`requests.empty_completion_recovered` in `health.json`), otherwise the request ledger fails with cause `empty-completion` and the user gets a typed retry prompt (`requests.empty_completion_failed`). Warning logs carry the provider class name and user/chat ids only — never answer bodies.

External waits (#740): an agent's "I'll continue once CI finishes" is backed by a durable registry at `<bot_data_dir>/external-wait/waits.json` (owner-only, previous-good backup). The bridge monitor polls GitHub checks pinned to the registered exact head SHA, journals terminal transitions before waking, notifies the owning conversation, and resumes through a bridge-owned `external_event` turn (autonomous-metered). Operators inspect with `/waits` and cancel with `/cancelwait <wait_id>`; agents register via `python -m telegram_bot.core.external_wait_cli register` (see the `gh-ci-wait` skill). Kill-switches: `CCC_EXTERNAL_WAIT_ENABLED=0`, `CCC_EXTERNAL_WAIT_RESUME=0`, `CCC_EXTERNAL_WAIT_RESUME_DAILY_CAP` (default 10/day). Records and logs stay body-free — no prompts, tokens, or check logs.

Webhook nudge (#1222, off by default): the wait monitor's backoff caps at 300s, so a CI run finishing late in the window sits undetected for up to five minutes. `CCC_WEBHOOK_NUDGE_ENABLED=true` starts a loopback-bound listener (`CCC_WEBHOOK_NUDGE_HOST`/`_PORT`, default `127.0.0.1:8791`, path `/nudge`) that accepts HMAC-signed GitHub webhook deliveries (`workflow_run`, `check_suite`, `pull_request`) and pulls the matching waits' next poll forward to "now". The payload is treated strictly as an untrusted hint: terminal classification, exact-head validation, wake journaling, and resume budgets all remain in the polling monitor, so a forged delivery can at most trigger one early authenticated `gh` read and a lost delivery degrades to today's polling behavior. `CCC_WEBHOOK_NUDGE_SECRET` is required — enabling without it refuses to start the listener (fail-closed) while the bridge boots normally (keep the value in the node's env file, never in the repo). Public ingress from GitHub to the loopback listener (tunnel/reverse proxy) and registering the webhook on the repo are per-node operational decisions outside the bridge; payload bodies are parsed in memory and never persisted.
