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

## Provider environment contract

Some keys are read only from the provider CLI's own process environment: the
`ccc-piri`/`ccc-codex` wrappers resolve the real CLI through
`CCC_PIRI_REAL_CLI_PATH`/`CCC_CODEX_REAL_CLI_PATH`, and the Claude CLI
authenticates from `CLAUDE_CODE_OAUTH_TOKEN`/`ANTHROPIC_*`. `Config.load`
reads the project and package `.env` files without exporting them, and the
Matrix unit (which runs `python -m telegram_bot`, not `start.sh`) never gets
`start.sh`'s `bridge/.env` export (#1771).

**Two routes, by kind of key.**

- *Wrapper paths/switches* (`WRAPPER_ENV_KEYS` in
  `bridge/utils/wrapper_environment.py`: `CCC_PIRI_REAL_CLI_PATH`,
  `CCC_PIRI_MEMORY_*`, `CCC_CODEX_REAL_CLI_PATH`,
  `CCC_CODEX_MEMORY_MATERIALIZER_PATH`) may live in the project `.env` or
  `bridge/.env`: the bridge hands them from the merged config to the wrapper
  child (#2065). Process environment still wins.
- *Everything else the CLI reads only from its environment* — above all
  secrets such as `CLAUDE_CODE_OAUTH_TOKEN` — is never injected by the
  application. It must be in the bridge **process** environment, i.e. the
  shared EnvironmentFile below (or `bridge/.env` exported by `start.sh`, which
  covers the Telegram unit only).

**Shared EnvironmentFile.** Both systemd units read one owner-only file:
`EnvironmentFile=-%h/.config/ccc-node/bridge.env` — the Telegram unit rendered
by `bridge/service-systemd.sh` (literal `$HOME` path) and
`bridge/service-systemd-matrix.service.example` (`/root/...`). The leading `-`
keeps a node without the file on its previous environment. Keep secrets there
(0600), **never** in an `Environment=` line (unit files and `systemctl show`
are world-readable). `EnvironmentFile=` overrides `Environment=`, so never put
`HOME`, `PATH`, `PROJECT_ROOT`, `BOT_DATA_DIR`, `CCC_CHANNEL` or `CCC_MATRIX_*`
in it. Keep each key in exactly one place: `start.sh` (Telegram) re-exports a
`bridge/.env` value over the process environment for keys outside its
preserve list, while the Matrix unit keeps the EnvironmentFile value, so a key
present in both with different values diverges between the two frontends.

**Startup required-env check.** Right after logging starts the bridge checks
what the selected provider needs — against the process environment plus the
#2065 wrapper keys, i.e. what the child will get — and logs one ERROR with key
**names** only, e.g. `required provider environment missing: provider=piri
missing=CCC_PIRI_REAL_CLI_PATH`:

- piri/codex: the configured CLI (`CCC_PIRI_CLI_PATH`/`CCC_CODEX_CLI_PATH`) must
  resolve on the child `PATH`; when it is the `ccc-*` wrapper, the real CLI it
  execs must resolve too.
- claude: one of `CLAUDE_CODE_OAUTH_TOKEN|ANTHROPIC_API_KEY|ANTHROPIC_AUTH_TOKEN`
  in the process environment, unless a non-env login exists
  (`.credentials.json`, `apiKeyHelper`, Bedrock/Vertex/Foundry flags, macOS
  keychain).

The bridge then runs **degraded**, not stopped — the same policy as the existing
provider readiness probes. Exiting would crash-loop the unit under
`Restart=always` (or exhaust `start.sh`'s rapid-crash budget) and stop push-spool
delivery, the owner's alert channel. Telegram reports the message as the probe
reason (`Service: degraded (...)`; for Claude it is appended to a failed
`claude auth status`, which stays authoritative); the Matrix frontend, which has
no provider probe, records it as `health.json` `agent.last_error` instead of
marking the agent healthy. A successful turn clears it. The provider's own
startup failure cause (exit code, redacted stderr tail) is logged by #1819.

**Migration (per node, operator-run; not automated).**

1. `install -d -m 0700 ~/.config/ccc-node && install -m 0600 /dev/null
   ~/.config/ccc-node/bridge.env` (as the unit's user; `/root` for system units).
2. Move the provider/auth lines from the Telegram and Matrix drop-ins
   (`*.service.d/provider.conf`, `zz-piri.conf`, ...) into it, plus any env-only
   secret such as `CLAUDE_CODE_OAUTH_TOKEN`. Remove the same keys from
   `bridge/.env` (one place per key, see above). Check `stat -c %a` is `600`.
3. Refresh the units: `./setup.sh` (or `bridge/service-systemd.sh reconcile`)
   rewrites a ccc-generated Telegram unit with the `EnvironmentFile=` line and
   only daemon-reloads; add the line to the hand-installed Matrix unit from the
   example. Run `systemd-analyze verify <unit>` on both — a drop-in without its
   `[Service]` header is otherwise ignored silently.
4. Delete the now-empty drop-ins, `systemctl daemon-reload`, then restart both
   bridges at an idle moment (see the occupancy check above).
5. Verify without printing values: both bridges' `bot.log` show no
   `required provider environment missing`, and
   `tr '\0' '\n' < /proc/<pid>/environ | cut -d= -f1 | sort` lists the same
   provider key names for the Telegram and Matrix PIDs.

Rollback: restore the drop-ins; the optional `EnvironmentFile=-` line is inert
once the file is removed.

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

Coverage check (#1229 follow-up): the nudge hook is registered **per
repository**, so every new repository needs one more hook — a rule that
otherwise lives in memory (2026-10-02: 6 of 42 repositories had none, three
of them weeks old). `scripts/nudge-hook-coverage.sh --org <org> --url-pattern
<relay-url-substring>` lists each repository with its matching hook ids, skips
repositories without CI workflows, and exits `10` when a CI repository has no
hook; `--comment owner/repo#<issue>` posts a body-free summary to the tracker
only when gaps exist. It needs an admin-scoped `gh` session (hook listing is
admin-only), so run it where that session lives — the relay node — e.g. weekly
from cron with `GH_CONFIG_DIR` pointing at that session. It never creates
hooks; registration stays a separate, approved step.

## Group rows still holding a DM session (#2075)

Before #2092, a group/room row in `sessions.json` could end up holding the
sender's DM session id: the first-use seed copies the legacy unscoped
`<uid>` row into a new scoped row, and the old external-wait/continuation
runners resumed the DM session on the room's stream, which the next room turn
then saved. #2092 fixed the lookups, not the rows already written, so such a
room keeps resuming a DM-derived session. `ccc-doctor` reports the count as
the `session scope rows` warning (counts only, read-only).

Detection is store-only and independent of `CCC_TELEGRAM_SESSION_SCOPE`: a
room row (`<uid>:<chat>` or `0:<chat>`) is flagged when its `session_id` is
also held by a DM/legacy `<uid>` row (`dm-session`) or by another room
(`cross-room-session`). Two scope keys of the same room are not flagged; the
`0:0` shared-all row is ignored. Once the DM row has moved on to a new
session the store keeps no trace — the #2074 sidecar `ambiguous` count is the
remaining signal for those.

```bash
python3 scripts/ccc_session_scope_audit.py            # dry-run: counts + row keys
# stop the bridge that owns the store first (both frontends if both are flagged)
python3 scripts/ccc_session_scope_audit.py --apply    # backup, then clear
```

Default stores are `$BOT_DATA_DIR/sessions.json`, `~/.telegram_bot/sessions.json`
and `~/.ccc-matrix/sessions.json`; pass `--store PATH` (repeatable) otherwise.
`--apply` gives each flagged room row exactly what `/new` persists
(`session_id: null`, `new_session: true`) and keeps its other fields; the row
is not deleted, because an empty row would be re-seeded from the DM row on the
next turn. DM rows are never modified. It refuses while the bridge owning the
store is running (`bot.pid`) and while a pending external wait or continuation
in a flagged room is still bound to a flagged session id (its runner would fall
back to that registered id; let it finish or cancel it), and it copies the audited bytes to
`sessions.json.bak-2075-<utc>` (0600) before an atomic write. A second run is a
no-op. Roll back by restoring that backup with the bridge stopped. The room's
next turn starts a fresh session; the DM-derived transcript is not distilled
under the room's audience.
