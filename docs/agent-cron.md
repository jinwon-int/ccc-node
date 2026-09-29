# Agent-cron operations

`agent-cron` is the local durable task scheduler surface for ccc-node. It is intentionally conservative: status and planning modes are read-only, and timer installation is a separate explicit step.

## Commands

- `scripts/agent-cron.sh add <task-id> --schedule EXPR --prompt TEXT [flags] [--json]` —
  create a task (validated, atomic write; no execution). See `--help` for flags
  (timezone, notify, allowed-tools, `--success-exit-codes 0,1`, payload `--argv/--cwd/--model/--timeout-sec`,
  `--not-before`, `--max-runs`, `--keep-after-run`, `--disabled`, ...).
- `scripts/agent-cron.sh edit <task-id> [same flags as add] [--json]` — set-only
  partial update (schedule/timezone re-validated; payload flags merge, `--argv`
  replaces the whole argv). No clear semantics — unset by remove+add.
- `scripts/agent-cron.sh remove|enable|disable <task-id> [--json]` — store-only
  mutations; never execute, install timers, or send messages.
- `scripts/agent-cron.sh list [--json]` — inspect configured tasks.
- `scripts/agent-cron.sh due [--at ISO8601] [--json]` — read-only due/retry resolver.
- `scripts/agent-cron.sh status [--at ISO8601] [--json]` — read-only operator rollup for task health, retry wait/exhaustion, lock state, and last run state.
- `scripts/agent-cron.sh run <task-id> --dry-run` — execution-plan preview only.
- `scripts/agent-cron.sh scheduler --dry-run` — one scheduler tick preview.
- `scripts/agent-cron.sh scheduler --execute` — explicit one-shot execution path for an already-approved scheduler unit.

If headless work completes but its run-state commit fails, agent-cron reports
`status=persist-failed` and converts that run's lock into a non-expiring
quarantine. This prevents the next scheduler tick from executing and notifying
the same occurrence again. After repairing the task store, inspect the holder
with `lock <task-id> --action probe --json`, then explicitly clear it with the
matching run id: `lock <task-id> --action release --run-id <run-id> --json`.

## Headless runners

The installer defaults to the existing Claude runner. Use `--runner codex` to
install the ephemeral Codex runner instead. Codex defaults to
`CCC_CODEX_SANDBOX=read-only`, sets non-interactive approval policy to `never`,
and never persists a Codex session. Broader sandboxes require an explicit
`--codex-sandbox` choice at installation time.

Tasks may set `maxRuns` to a positive integer. For those bounded tasks, every
completed headless invocation, successful or failed, increments durable
`runCount`; reaching the limit disables the task and cancels any pending retry.
This makes `maxRuns: 1`
safe for one-time LLM jobs without leaving an annually recurring cron enabled.
Set `notBefore` to the intended UTC activation timestamp so a newly-created
annual cron expression cannot catch up an occurrence from the previous year.
For example, a July 22 one-time job uses its normal five-field schedule,
`notBefore: 2026-07-22T02:27:00Z`, and `maxRuns: 1`.

## Schedule forms

`schedule` accepts four kinds (epic #584-adjacent cron upgrade, referencing the
Hermes and OpenClaw schedulers):

- **Cron:** 5-field expression or `@hourly|@daily|@weekly|@monthly|@yearly`,
  matched in the task's `timezone` (IANA name, e.g. `Asia/Seoul`; default UTC).
  Each field takes `*`, a value, a range, a comma list, and an optional `/S`
  step — `0 9 * * 1-5` (weekdays 09:00), `*/30 9-17 * * *` (half-hourly during
  business hours). Alphabetic names (`MON`, `JAN`) are not supported; use the
  numeric equivalents, with day-of-week `0`/`7` both meaning Sunday.
- **Interval:** `every <N>m|h|d` (min 1 minute, max 366 days). Free-running from
  `lastRunAt`; set `anchorAt` (ISO8601) to phase-anchor occurrences
  (e.g. anchor `..T00:15Z` + `every 1h` fires at :15). A never-run interval task
  with no anchor is due immediately once.
- **One-shot:** `at <ISO8601>` or a bare ISO8601 timestamp. Naive timestamps are
  anchored to the task `timezone`. After a successful run the task is
  auto-disabled unless `keepAfterRun: true`.
- Unknown timezones and malformed expressions fail closed as
  `invalid-schedule` in `due`/`status` output; `due` rows expose `scheduleKind`.

## Payload kinds

Each task runs one payload (default: `prompt`, backward compatible):

- **prompt** (default): the existing headless Claude run of `prompt` via
  `claude/headless.sh`. Optional `payload.model` is passed through as
  `--model` (via `CCC_MODEL`). Wall-clock timeout `payload.timeoutSec`
  (default 3600s).
- **command**: `payload.argv` runs directly (no shell interpolation, no LLM
  token spend — watchdog/maintenance jobs). Optional `cwd`,
  `timeoutSec` (default 600s), `outputMaxBytes` (default 64 KiB, capped
  capture). `model` is rejected for command payloads.

A timed-out run records status `timeout` (exit code 124) and consumes the
normal retry policy. Cross-field payload rules (argv required for command,
argv/cwd rejected for prompt) are enforced fail-closed by `validate` and on
load.

By default only exit code `0` counts as success. A watch-type task that
**exits non-zero to signal findings** (e.g. a fleet node is DOWN) would be
mislabeled `failed` → `retry-exhausted`. Set `--success-exit-codes 0,1` so
exit 1 is recorded as a successful run-with-findings; only codes outside the
set (2+, 127, 124-timeout) count as `failed`. `status` shows `lastExitCode`
so an operator can tell `1` (findings) from `127` (command missing) at a
glance. A task that declared **no** `retryPolicy` has no retry concept and is
never labelled `retry-exhausted` — its failures stay plain `failed`.

## Notify modes

- `none` (default) — no spool writes.
- `telegram-owner` — every run writes a short redacted owner-only spool entry.
- `telegram-owner-on-failure` — spool only non-success runs (failed/timeout);
  successful runs report `delivery: skipped-success`.
- `telegram-chat` / `telegram-chat-on-failure` (#665) — deliver to a specific
  group/channel chat instead of the owner DM. Requires `--notify-chat-id <id>`
  (numeric group id like `-1001234567890`, or `@channelusername`); the schema
  fails closed if the chat target is set without an id. The chat id must be on
  the **allowlist** `CCC_AGENT_CRON_NOTIFY_ALLOWED_CHATS` (CSV/JSON) — an
  out-of-allowlist target reports `delivery: blocked-not-allowlisted` and writes
  nothing. The spool record adds `recipient: chat` + `chatId`; the same
  spool/redaction/audit path as owner delivery is reused (no per-task token
  handling). The bridge push notifier **re-validates** the chat id against the
  same allowlist on read before sending (defense in depth), so a forged spool
  file can never reach an un-allowlisted chat.

Owner/chat spool text uses the canonical credential patterns from this
checkout's `bridge/utils/redaction.py`, followed by the deliberately broader
agent-cron owner-spool masks for short bearer/assignment/near-token values and
arbitrary long runs. Redaction happens before the display-length cap. If the
canonical module cannot be loaded, task execution and history remain intact but
notification delivery reports `blocked-redaction-unavailable` and writes no
captured output to the spool.

For a non-success run, the already-redacted, bounded stdout/stderr are also
checked for the exact line-start fleet diagnostic tokens `DOWN`, `UNREACHABLE`,
`DRIFT`, and `BOOTPATH`. If any are present, the first line identifies a fleet
alert and includes only the validated task id plus deterministic token counts;
node names, paths, credentials, and the rest of each diagnostic row remain out
of the title. Failures with no recognized signal keep the generic status first
line, and successful notifications keep their existing text. This shared
formatting applies to existing command tasks such as `adapter-fleet-watch` and
`fleet-doctor-sweep` without a task-store field or migration.

## Failure classes and the consecutive-failure alarm (#1821)

Every non-success run gets a bounded `failureClass` — `auth_failed`,
`cli_missing`, `timeout`, or `other` — from the exit code, the runner spawn
error, and stderr only (never stdout or the model's result text). `timeout` is
status `timeout` or exit 124; `cli_missing` is exit 126/127 or a runner that
could not be spawned; `auth_failed` matches provider login errors on the
runner's own stderr (`authentication_failed`, `Not logged in`, invalid/expired
key, `401 … Unauthorized`), and inside the stdout block that `ccc-headless`
echoes to stderr only an unescaped `"error":"authentication_failed"` field.

**Nothing new is written to `tasks.json`.** The per-run class ledger (`runs`,
bounded to 50), each task's `lastSuccessAt`, and the alarm counters live in
`failure-alarm.json` next to the store (0600, updated under the store lock).
The task store therefore stays valid under the pre-#1821 schema, so reverting
this feature cannot make the scheduler (including the self-update task) refuse
its own store. The only new task field is the operator-set, optional
`failureAlertAfter`. **Before reverting this feature, remove
`failureAlertAfter` from every task in `tasks.json`**: the pre-#1821 schema
rejects it (`additionalProperties: false`) and the scheduler would refuse the
store.

Two counters, each bounded so no pattern of outcomes alerts on every run:

- **Task counter** — the task's own consecutive failures. One owner alarm when
  it reaches N (default 3). Within the same streak it re-alerts only when the
  class changes **to** `auth_failed` or `cli_missing`, at most once per 24h;
  flapping between other classes (exit 1 ↔ 124, timeout ↔ other) stays quiet.
  One "cleared" notice on the next success.
- **Node counter** — consecutive prompt-task failures across tasks. One alarm
  per streak, only when the streak reaches N **and spans at least two distinct
  tasks** (one broken task is the task counter's job); never re-alerts on a
  class change. A success by a task outside the streak does not clear an
  alerted streak; one "cleared" notice when a task that was part of it
  succeeds. The message names the failing tasks. This catches the incident
  shape, where four different one-shot prompt tasks each failed once. On every
  prompt run an alerted streak's task list is pruned to tasks that can still
  run (present, enabled, under `maxRuns`, not a finished one-shot); if none
  remain, nothing could ever send its "cleared" notice, so the counter resets
  **silently** and the next streak can alert again. A streak that has not
  alerted yet is never pruned (finished one-shots are the incident evidence).

A run where both counters fire produces one message. Alarms go to the owner
through the same push spool as `notify=telegram-owner`, carry only task ids,
classes, counts and timestamps, and never run output. The state is persisted
with the alert marked sent **before** the spool write: if the state cannot be
written the alert is suppressed with a stderr warning (failing quiet, not once
per run); if the spool write fails after that, the one alert is lost.

**Semantics change:** the alarm fires regardless of the task `notify` setting,
including `notify: none` — the incident was exactly such a silent task. Opt out:

- per task: `failureAlertAfter: 0` (`--failure-alert-after 0`) — the task then
  feeds neither counter. A positive per-task value sets that task's own N
  only; the node counter uses the node-wide default;
- node-wide: `CCC_AGENT_CRON_FAILURE_ALERT_AFTER=0` (a positive value changes
  the default N for both counters).

Upgrade notes: a counter without state is seeded from existing `runHistory`,
so tasks already mid-streak alert on their **first** failure after upgrade (a
one-time burst of at most one message per task plus one node message). Command
tasks count too: e.g. the self-update task accepts exit 0/8/11, so its exit 3
(lock held) and 14 (restart/activation failure) are failures and three in a row
alert once.

`ccc-doctor` reports `agent-cron prompt success` (warning; D = 7 days,
`CCC_DOCTOR_AGENT_CRON_STALE_DAYS`). It resolves the store exactly like
agent-cron (`CCC_AGENT_CRON_STORE`, else `~/.claude/state/agent-cron/tasks.json`)
and reads `lastSuccessAt`/class from `failure-alarm.json`, falling back to
`runHistory`. Two bounded verdicts: an enabled recurring prompt task whose
newest run failed and whose last success is older than D days; and a node
verdict when prompt runs are still being attempted (newest within D days) and
failing with no prompt success anywhere for more than D days. Disabled and
one-shot tasks never get a per-task warning, so nothing warns forever.

## Safety boundaries

Read-only/status modes never acquire locks, execute prompts, write bridge spools, install timers, edit crontab/systemd, send Telegram, call providers, or touch remotes. Execution mode may write task history and owner-only redacted spool entries, but still does not install timers or call Telegram/provider APIs directly. `add`/`remove`/`enable`/`disable` mutate only the validated task store via the same atomic private write path.

### Hang guards

A tick that never returns is the failure mode to design against: `Type=oneshot`
defaults to `TimeoutStartSec=infinity`, so one stuck run pins the unit in
`activating` and **the timer never fires again** until an operator intervenes.
Two independent caps exist:

- `TimeoutStartSec=` is always emitted by `install-agent-cron-systemd.sh`
  (default `1800`; override with `--timeout-start SEC` or
  `CCC_AGENT_CRON_TIMEOUT_START`, `infinity` to opt out).
- Both headless runners wrap the provider CLI in `timeout` — `CCC_HEADLESS_TIMEOUT`
  seconds (default `1500`, `0` disables), exiting `124` when tripped. This is the
  only cap on Termux nodes, which have no systemd.

Task prompts must not instruct the agent to block on an external condition.
An observed real failure: a task told the agent to wait for a PR to become
mergeable, and it emitted `until state=$(gh api …) && echo "$state" | grep -q
'"m":true\|"m":false'; do sleep 5; done`. The PR was then merged, GitHub began
returning `mergeable: null` for the closed PR, and the exit condition became
unreachable — the loop ran for 11 days. Prefer a bounded number of polls with an
explicit deadline, and treat "condition never became true" as a reportable
outcome rather than something to wait out.

## Source boundaries

- `schemas/agent-cron-task-store.schema.json` is the structural source of truth.
- `scripts/agent_cron_schema.py` applies the schema fail-closed without an optional
  system Python dependency; duplicate task IDs are the only store-level semantic
  rule layered on top.
- `scripts/agent_cron_model.py` owns pure task lookup and prompt-free list projections.
- `scripts/agent_cron_repository.py` owns validated load and private atomic writes.
- `scripts/agent_cron_lib.py` owns pure schedule and retry calculations.
- `scripts/agent_cron_alarm.py` owns pure failure classification and alarm transitions.
- `scripts/agent_cron.py` is an import-safe CLI composition root. Dispatch only runs
  through `main()`; importing it does not parse commands, print, or mutate the
  filesystem or process environment.

Planning functions remain read-only and produce explicit mutation metadata. The
runner applies locks, headless execution, history, retry state, and spool writes only
after an explicit `run` or `scheduler --execute` dispatch.

## Fleet closeout pattern

For fleet operations, collect each node's `status --json` output into evidence blocks and summarize only metadata: node, task id, `lastStatus`, `retryEligibleAt`, retry exhaustion, lock state, and safe error class. Do not collect prompts, memory contents, raw env, tokens, chat IDs, or provider output.
