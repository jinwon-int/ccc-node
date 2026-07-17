# Changelog — ccc-node harness

All notable changes to the Claude Code node harness. Dates are KST.

## [Unreleased]

### Changed
- Claude Code now defaults to native `bypassPermissions` mode, matching the
  no-prompt execution posture used by ccc-node/Codex. New installs and
  self-updates receive the mode through `settings.base.json`; the independent
  ccc-node PreToolUse guard continues to enforce Fresh Approval Required
  boundaries, including catastrophic local `rm`.
- Opt-in Codex-parity ungoverned Claude execution
  (`CCC_BRIDGE_CLAUDE_UNRESTRICTED`, default **false**, `owner-operator` only).
  On a node that sets it true, the bridge's Claude SDK path runs with
  `permission_mode=bypassPermissions`, no OS sandbox, and no host settings
  chain — so the PreToolUse `guard.py` hook is not loaded — matching the
  Codex `never + dangerFullAccess` contract that `auto-approve` already maps
  to (`bot_access.py`). MEMORY/USER context is preserved through the curated
  settings block. The flag is fail-closed: `claude_unrestricted_enabled`
  ignores it on `strict-project`/`disabled` (which stay sandboxed) and honors
  only an explicit boolean-true, so it can never widen a non-owner node to
  host scope. Default keeps `guard.py` as the boundary; the change is
  per-node and reversible. This unifies the two providers' execution scope at
  the operator's explicit request without dropping the guard for nodes that
  do not opt in.
- Claude's service-lifecycle guard now treats detached Compose reconciliation
  (`docker compose up -d [services...]`, including `docker-compose` and
  `--detach`) as autonomous, matching the ccc-node/Codex recoverable path. The
  exact broker restart/rollback runbook may include literal `cd`, optional
  `docker tag`, one reconciliation, and read-only `inspect`/`sleep` (maximum
  300 seconds)/loopback-curl verification; direct SSH requires named fleet
  services. Other Compose lifecycle, multiple reconciliations, remote-daemon
  selection, wrappers, substitutions, arbitrary compounds, and
  external/mutating curl remain fail-closed. Refs #544.
- `gh-pr-flow` now handles protected merges that require an independent
  `jinon86` review of a `seoseo-ai` PR. A narrow helper uses Seoseo's existing
  authenticated session only after fresh, per-invocation explicit approval,
  validates remote actor/repo/author/base/state/reviewer scope, never extracts
  the token, and leaves the final squash merge on the normal account. Mock
  regression coverage verifies each gate.
- `gh-pr-flow` now includes a fail-closed Seoseo merge fallback for the case
  where local `seoseo-ai` lacks organization merge permission. Each use
  requires fresh PR-specific operator approval, keeps the existing `jinon86`
  credential on Seoseo, pins the exact head SHA, rejects draft/non-main/dirty
  merge state and pending or failed checks, and calls the squash-merge API
  without an admin bypass. The helper has deterministic no-approval,
  head-drift, failed-check, legacy-status, dry-run, and merge-path tests.
- Codex `/usage` now hides the `GPT-5.3-Codex-Spark` rate-limit bucket and
  account lifetime/daily token history, omits unavailable context/session rows,
  and renders reset timestamps deterministically in KST (UTC+9).
- Family Wiki log-writing guidance now allocates node/date-scoped
  `LOG-YYYYMMDD-<node>-<seq>` IDs under `[LOG-00]` instead of the collision-prone
  global `LOG-NNNN` max+1 scheme. The wiki skill, slash command, session
  cheatsheet, and durable-memory template share the rule, and harness validation
  rejects regressions to the old allocator. Refs
  `jinwon-int/seoyoon-family-wiki#2246`.
- PreToolUse guard rewritten from bash to `guard.py` (#452), invoked through a
  thin `guard.sh` shim so the hook/install contract is unchanged (stdin payload →
  exit 2 to deny). shlex tokenization replaces the hand-rolled bash word-splitting
  and its three derived string views; `guard.test.sh` (the executable-contract
  golden suite) drives the swap — every prior non-relaxed case passes unchanged.
  The shim fails OPEN only when python3/guard.py is unavailable (matching the
  historical missing-jq posture); `guard.py` fails CLOSED on internal errors and
  every matched gate. Supersedes the guard internals touched by the fleet-restart
  branch; `validate-harness.sh` now `py_compile`s guard.py and asserts setup.sh
  installs it.
- Service-lifecycle gate re-baselined for fleet operations: pure lifecycle verbs
  (start/restart/reload/stop/kill + try-/or- variants) on fleet units
  (`a2a`/`hermes`/`openclaw`/`broker`/`gateway`/`worker`/`ccc-telegram-bridge`) are
  autonomous, locally and toward a peer node (`ssh <node> systemctl restart <unit>`,
  `systemctl -H <node> …`). Still gated: non-fleet units, config verbs
  (enable/disable/mask/isolate/daemon-*) on the local node, `pm2 delete`,
  docker/podman/kubectl, and local host lifecycle. Compound commands are judged
  per statement; one non-fleet target denies the whole command.
- Remote fleet restart classification now inspects quoted SSH and shell-function
  bodies one command at a time, so read-only post-restart verification (`sleep`,
  `systemctl is-active`, `systemctl show`) no longer turns into false target names.
  Mixed non-fleet targets and config-changing verbs still fail closed. Refs #534.

### Added
- Node-local model-usage metering with daily budget caps (#388). The new
  `bridge/core/usage_meter.py` durably records body-free token/request
  counters per KST day × provider × interactive/autonomous mode in
  `.telegram_bot/usage-meter.json` (atomic owner-only writes, bounded
  retention, fail-open persistence). Spend sites wired: Claude interactive
  turns meter at the reader's `ResultMessage` using the complete validated
  input total (raw plus cache-creation and cache-read tokens), Codex
  interactive turns meter via a runtime usage recorder fed by cumulative
  `thread/tokenUsage/updated` deltas — threads the process created start
  from a zero baseline so their first turn is metered, and a resumed
  thread's first notification during a turn this process started derives an
  implied pre-turn baseline from the turn-scoped `last` block (total minus
  last) so the first post-resume turn is metered while prior-session
  history is never counted — plus one request per provider attempt,
  recorded at the runtime's spend boundary (the accepted `turn/start`) so
  every outcome including error terminals and turns cancelled before their
  first event charges exactly once while pre-boundary failures charge
  nothing, and the distill extraction worker charges every
  autonomous attempt with a worst-case pre-spend token reservation over the complete request
  (8192 prompt/schema overhead + the backend's hard output-size cap as the
  output allowance + six tokens per raw snapshot byte, covering canonical
  JSON escape expansion at ≤1 BPE token per serialized byte) until the exec
  backend can report actual usage, so repeated background work consumes —
  and eventually hits — the cap and complete valid input+output cost cannot
  exceed an admitted budget. Autonomous admission is atomic and prospective: the
  meter's `reserve_autonomous_spend` admits only when the whole bounded
  attempt cost (overhead + persisted snapshot size, reserved before the
  claim) fits under the cap and charges it in the same locked step, so
  concurrent attempts cannot jointly overrun the cap, a single oversized
  attempt is rejected outright, and the recorded autonomous total never
  exceeds the configured cap. Reservations are opaque day-pinned handles:
  the charge and any later refund target the accounting day captured at
  admission, so a midnight rollover can neither split a reservation across
  days nor let a refund erase another day's spend. No-op invocations (claim
  lost or job already done) refund their exact reservation, a tiny valid
  budget no longer warns at zero usage, and budgets must fit at least one
  maximal attempt or that work stays deferred by design. Every meter
  mutation additionally holds an exclusive interprocess file lock and
  re-reads the on-disk state before applying its delta, so overlapping
  meter instances or bridge processes merge spend instead of losing it to
  last-writer-wins (falling back to thread-only locking with a logged
  warning if the lock file is unavailable, and preserving unpersisted
  in-memory deltas across repeated save failures instead of reloading over
  them). The bridge
  composition root (`build_context`) now constructs the distill extraction
  worker itself through the handler factory with the shared meter, the
  running `TelegramBot` retains that gated instance and drives it from the
  bridge lifecycle: a fail-open sweep (default every 300s,
  `distill_extraction_poll_interval`) runs every ready snapshot job through
  the gated worker, so capped work is deferred before any provider call
  while job-creating trigger policy remains #465's phase, and the worker's `usage_meter` is an explicit required constructor
  decision.
  Optional per-provider daily token budgets
  (`CCC_USAGE_BUDGET_TOKENS_CLAUDE`/`_CODEX`, 0 = off) raise one warn (early
  alarm at `CCC_USAGE_BUDGET_WARN_PERCENT`, default 80%) and one enforce
  alert per provider-day; at the enforce threshold the distill worker defers
  autonomous extraction without claiming the job or burning an attempt while
  interactive user turns keep flowing by design.
  `ProjectChatHandler.build_distill_extraction_worker` is the composition
  root for #465's scheduling: it always injects the shared meter (callers
  cannot substitute their own gate), and budget alerts additionally queue an
  owner push through the opt-in push-notifier spool (log-only when
  `CCC_PUSH_ENABLED` is off). `/usage` now appends a compact 7-day local
  meter report with budget state. Metering never blocks or fails a turn
  (`CCC_USAGE_METER_ENABLED=false` disables it entirely).
- Provider conformance contract + capability matrix (#387). The new
  `bridge/core/provider_capabilities.py` is the single source of per-provider
  capability states (`supported`/`degraded`/`unsupported`/`unknown`, each with
  a machine-readable reason and issue dependencies) across 13 runtime axes and
  the 8 memory-parity axes; `docs/provider-capability-matrix.md` is rendered
  from it and golden-pinned. A shared `AgentRuntime` behavior suite
  (`bridge/tests/runtime_conformance.py`) now executes the contract — session
  lifecycle, streaming delivery with result-before-completion terminal
  ordering, tool-event pairing, fail-closed approvals, interrupt/liveness,
  error normalization, and per-session turn serialization — against the real
  `CodexRuntime` over a scripted fake app-server plus a normative reference
  runtime, with negative tests proving each contract violation fails the
  suite. Drift checkers pin the matrix to the session layer's provider set,
  the runtime/usage/memory-hook surfaces, and the executable coverage, so new
  provider adapters (#354 successors) must pass the suite and update the
  matrix to land.
- A durable provider-neutral Codex distill extraction worker now advances completed
  snapshot jobs through fenced extraction leases, atomically retains one strict result,
  classifies body-free retryable/terminal failures, and recovers cancellation or stale
  leases without re-reading the user thread. Runtime scheduling and memory sink writes
  remain deferred under #465. Refs #532.
- Hermes-style unattended skill-autosave **auto mode** (#355, opt-in via
  `CCC_SKILL_AUTOSAVE_MODE=auto` or the `skill-autosave.mode` state file;
  default `approve` keeps every existing node unchanged). New
  `claude/hooks/skill-review/autoinstall.sh` replaces the human approval gate
  with deterministic machine gates — secret scan (redaction pattern family,
  hard-fail), node-specific-fact scan (home/root paths, non-loopback IPs,
  user@host), dedup vs installed skills (never overwrites), and HARDLINE-style
  structure lint — then installs passing drafts into `~/.claude/skills/` with
  an `installed-by=autosave` ledger + in-dir marker, a daily install cap
  (`CCC_SKILL_AUTOSAVE_DAILY_CAP`, default 3), and a post-hoc (not approval)
  Telegram notice for installs and blocks. Gate failures are never dropped:
  drafts stay pending with a recorded reason for the normal human path. Both
  draft layers drive it (SessionEnd hook immediately, daily sweep as backstop);
  `/skill-suggest` gains the post-hoc role — `list` / `rollback <name>` /
  `rollback --all` (archive-only undo that refuses unmarked, hand-authored
  skills). Off-switch honored; template-repo skills remain PR-first.
  credential-redacted input with an explicit untrusted-content marker, recursive
  unknown-field rejection, bounded Honcho/Wiki/resume output, safe relative Wiki
  targets, directive/credential fail-closed gates, body-free diagnostics, and a
  checked-in JSON Schema. This source-only phase exposes `DistillBackend` but makes
  no provider call, journal transition, or sink mutation.
- Managed-nodes allowlist for owned-node writes (opt-in, fail-closed). Operator-owned
  `~/.claude/managed-nodes.allow` (override `CCC_MANAGED_NODES_ALLOW`) lists the remote
  hosts this node operates. For a Bash statement whose only remote reach (via
  ssh/scp/rsync/sftp/`systemctl -H`) is to a listed host, `guard.py` relaxes the
  blast-radius gates for that statement — secret/key deployment (`scp deploy.env node:`),
  remote cleanup (`ssh node "rm -rf /var/log/old"`), remote service config verbs
  (`ssh node "systemctl daemon-reload"`), and **reboot** of the host (`ssh node reboot`).
  Reboot-class (`reboot`/`shutdown -r`, recoverable) is also relaxed on the LOCAL node;
  the down-class (`poweroff`/`halt`/`shutdown` without `-r`) stays gated everywhere
  (incl. managed nodes) since a powered-off node stays offline unattended, and reboot of
  an unlisted host / interpreter-mediated forms stay gated. Fail-closed everywhere else:
  no allowlist → fleet-only baseline;
  an unlisted host → fully gated; curl/wget/nc excluded (secret-exfil keeps authority);
  review-gated classes (force-push to protected, history-rewrite, release, DB) never
  relaxed even via ssh; a local destructive op chained beside a managed remote op is
  judged on its own statement and denied. The allowlist is agent-write-gated
  (`managed-nodes-config`). Quote-aware statement splitting (a shlex-rewrite payoff) keeps
  a remote command chained inside quotes (`ssh node "a && b"`) intact as one managed
  statement. `guard.test.sh` grows managed-node/reboot/adversarial coverage;
  `docs/examples/managed-nodes.allow.example` documents the format. Refs #341, #452.
- Managed-services allowlist for local self-managed apps (opt-in, fail-closed).
  Operator-owned `~/.claude/managed-services.allow` (override `CCC_MANAGED_SERVICES_ALLOW`)
  lists the node's own non-fleet local units/containers/processes. `systemctl`/`service`/
  `pm2`/`docker`/`podman` lifecycle is relaxed when EVERY target of the command is listed;
  a mixed/unlisted target, targetless `daemon-reload`, Compose lifecycle other than the
  separately approved direct local detached `up`, a docker remote-daemon flag, and
  command-substitution targets stay gated, and `kubectl` is never relaxed — so
  `sshd`/`ufw`/`nginx` stay protected while the node's own apps become self-manageable.
  Trailing `.service` is tolerated in matching. Write-gated for agents
  (`managed-services-config`); `docs/examples/managed-services.allow.example` documents it.
- Fail-closed external-node memory isolation (#466):
  `CCC_NODE_ISOLATION_PROFILE=external` provides a higher-priority bridge-to-hook
  placement policy and PreToolUse Family-resource guard; `CCC_WIKI_MEMORY_ENABLED=0`
  disables Family Wiki injection, refresh, local
  indexing (including stale distill artifacts), extraction candidates, and queue
  writes while preserving built-in/local/Honcho/resume memory. Node-local
  `CCC_MEMORY_USER_LABEL` / `CCC_MEMORY_ASSISTANT_LABEL` replace hard-coded
  relationship identities without changing existing defaults.
- Tag-based versioning preparation for #251: `scripts/ccc-version.sh`, ccc-doctor
  harness-version reporting, fleet-matrix version extraction, release workflow,
  and CONTRIBUTING release policy. Actual tag/Release publishing remains a
  separate operator approval gate.

### Notes
- Future release tags should be `v0.MINOR.PATCH`. Historical changelog headings
  without a leading `v` are preserved as-is; the release workflow accepts either
  tagged (`v0.4.0`) or historical (`0.3.18`) headings when extracting notes.

### Fixed
- Codex assistant replies lost paragraph spacing in Telegram (turning the readable
  flags on changed nothing). Codex streams text as `item/agentMessage/delta` chunks
  joined with no separator (`"".join` in `project_chat_process`, and the streaming
  accumulator), and the `item/completed` boundary that ends each assistant message
  was dropped in the runtime — so consecutive messages fused (`…다.현재…`), unlike the
  Claude path which sources `msg.result` with its `\n\n` intact. Since the readable
  renderer only *widens existing* blank lines, it could not restore a break Codex
  never emitted. `core/codex_runtime.py` now emits a `\n\n` text delta on a completed
  `agentMessage`, gated by an `emitted_text` flag so the separator only follows real
  text, never leads, and an empty message cannot double it. It rides the existing
  delta stream, so both the non-streaming join and the streaming accumulator recover
  the boundary and the renderer can space paragraphs. Regression coverage in
  `tests/test_codex_runtime.py`.
- `ccc_doctor.py --json` stdout is now strictly machine-parseable (#404):
  `scripts/ccc_doctor.py --json` runs the whole diagnosis with the real stdout
  file descriptor redirected to stderr and writes the single JSON document to a
  preserved private copy of stdout. This structurally prevents a probe (or a
  descriptor-inheriting subprocess such as a Codex helper) from trailing
  non-JSON bytes after the report, so a strict `json.load` consumer no longer
  fails with `Extra data`. Probe diagnostics surface on stderr; exit-code
  semantics (0 for 정상/경고 only; 1 for 교정가능/수동필요) are documented in
  `--help` and the module docstring. Regression coverage in
  `scripts/ccc-doctor.test.sh` parses the Codex `--json` path repeatedly with
  `json.load` and proves the stdout guard diverts stray fd-1/print leaks to
  stderr.
- CodeQL/required-check drift (#350): Dependabot now groups the complete
  `github/codeql-action/*` family, CodeQL publishes the stable
  `codeql-python` context, and `.github/required-checks.json` plus regression
  tests bind all six desired `main` checks to explicit workflow job names.
  The CI governance runbook distinguishes runner infrastructure failures from
  source failures and documents the narrow post-merge protection update.
- Canonical provenance/update drift (#351): bridge install docs now clone
  `jinwon-int/ccc-node`; `bridge/start.sh --version` uses
  `scripts/ccc-version.sh`, and `--upgrade` validates the canonical origin,
  pins `main`, and delegates to `scripts/ccc-self-update.sh` while preserving
  its terminal exit code. Historical bridge/package versions no longer drive
  runtime update decisions.

Distill observability follow-up — closes #130, #133.

A2A mobile native worker first slice — refs #150.

Native worker accepts the single-shot patch bridge — refs #150, a2a-nexus #1020/#1021.

### Added
- `scripts/a2a_termux_native_worker.py` now accepts `claude-a2a-patch-bridge.mjs`
  (a2a-nexus #1021) for `OPENCLAW_BIN`/`A2A_OPENCLAW_ANALYSIS_BIN` as an
  intent-aware drop-in superset of the analysis bridge, and validates an opt-in
  `A2A_CLAUDE_CODE_PATCH_MODE=single-shot` (fail-closed if set without the patch
  bridge). `WORKER_METADATA_JSON` `adapter` must now match the wired bridge.
  Env example + `docs/a2a-claude-worker.md` document the single-shot path; new
  test cases cover the patch-bridge env, mode/bridge mismatch, and bad mode value.
- `scripts/a2a-termux-native-worker.sh` + Python checker: validates a
  systemd-style env file for running `a2a-broker-worker/dist/worker.js` under
  Termux native/glibc-runner Node, with fail-closed bridge metadata, local
  tunnel, and env-hygiene checks. `run` is explicit and no live cutover or
  restart is performed by default.
- `scripts/a2a-termux-native-worker.sh` now also owns the singleton
  SSH-tunnel + worker-respawn supervisor (`supervise`/`stop`/`status`
  subcommands) — canonical replacement for the hand-rolled
  `~/.hermes/scripts/native-worker-supervisor.sh` that gongyung and daegyo
  used to run. Wiki ND-1236: singleton via `flock -n` (second supervise
  exits rc=3), orphan-safe cleanup (`cleanup_orphans` sweeps parent=1 ssh
  on port 18790; `kill_tree` walks pgrep -P recursively; `sweep_lingering_ssh`
  is the belt-and-suspenders finalizer). Supervise inherits
  `A2A_TUNNEL_SSH_TARGET` and `A2A_TUNNEL_REMOTE` env keys.
- `scripts/a2a-termux-native-worker-health.sh` (new, cron-safe): read-only
  supervisor / tunnel / worker snapshot with optional `--self-heal` (spawns
  a supervisor via `setsid -f` when none is running) and a
  `--max-supervisors N` cap detector that flags the exact >1-supervisor
  pile-up that motivated ND-1236, matching BOTH the canonical script AND
  the legacy `native-worker-supervisor.sh` process name so a pre-migration
  node running both trips the check. Exit codes 0/2/3/4/5 are distinct so
  cron logs are self-explanatory; `--json` emits a single-line schema.v1
  summary for fleet log ingestion.
- `docs/examples/a2a-termux-native-worker.env.example` and
  `docs/a2a-claude-worker.md` native-Termux section documenting the PR-first
  mobile worker path.
- `scripts/validate-harness.sh`: OpenClaw runtime/bootstrap context-file guard
  for tracked `AGENTS.md`, `SOUL.md`, `USER.md`, `TOOLS.md`, `HEARTBEAT.md`,
  `IDENTITY.md`, and `.openclaw/**` paths.

### Changed
- `claude/hooks/distill.sh` (#130): the three `skip reason=…` log lines
  (`no-transcript`, `cwd-out-of-scope`, `too-few-turns`) now emit
  `trigger=$TRIGGER pid=$$` so `/distill stats` can attribute them to the
  correct trigger column instead of falling through to `unknown:`.
- `claude/skills/distill/SKILL.md` (#130): the stats awk now caches
  `pid → trigger` from `start trigger=… pid=…` lines (plus a parent→bg PID
  bridge from `spawned bg pid=…`) and falls back to that cache when a
  downstream line lacks an inline `trigger=` field. Truly historical
  orphan lines (no trigger, no pid) still bucket into `unknown:`.
- `claude/hooks/distill/wiki-queue.sh` (#133): replaced the title-only
  hash with `title_hash()`. Strategy A — if the title contains one or
  more `#NNN` tokens, the hash is determined by the sorted set of issue
  numbers (so `#82 ...`, `Issue #82: ...`, `이슈 #82: ...`, `... (#82) ...`
  all collapse). Strategy B — sigilless titles get aggressive
  normalization (lowercase, strip bilingual section prefixes with colon,
  strip space-bounded `r\d+` round tags, replace common punctuation with
  space, collapse whitespace). The `.seen` file format is unchanged — old
  rows live their 7-day TTL and roll off naturally.

### Tests
- `claude/hooks/distill/wiki-queue.test.sh` — new cases covering
  issue-anchored cluster collapse, multi-issue distinctness, sigilless
  variant dedup (round-tag + punctuation), section-prefix bilingual
  dedup, and a HOT-crossing chain proving the dedup signal now feeds
  the existing `#76` hot mechanism end-to-end.

## [0.3.18] — 2026-06-22

Distill Tier-1 follow-up bundle — closes #71, #72, #73 in one PR.

### Added
- `claude/hooks/distill/queue-drain.sh` (#71): SessionStart-backgrounded retry
  worker for `honcho-queue.jsonl`. Reads up to `CCC_DISTILL_DRAIN_BATCH`
  (default 20) entries per run, retries each with the same upsert-session +
  POST-messages sequence as `honcho-push.sh`. In-band `_attempts` counter
  on each line; entries that exceed `CCC_DISTILL_DRAIN_MAX_ATTEMPTS`
  (default 3) move to `honcho-queue.jsonl.dead` for manual review.
  Pre-flight `/health` probe (skips drain if Honcho is unreachable).
  Single-flight via `flock` so concurrent SessionStarts don't double-drain.
  Replayed messages carry `metadata.replay: true` and a `(replayed)` marker
  in content so they're identifiable in Honcho.

### Changed
- `claude/hooks/distill/extract.sh` (#72): timeout (ec=124) now triggers a
  one-shot retry path — rebuilds the transcript window with halved
  `MAX_TURNS` and `MAX_BYTES`, calls `claude -p` again with the existing
  `STRICT` system prompt. Transcript construction was factored into a
  `build_redacted()` function so both attempts share the same redact +
  byte-cap logic. JSON-drift retry path (#70) is preserved and now lives
  after the timeout-retry branch.
- `claude/hooks/distill.sh`, `distill/honcho-push.sh`, `distill/wiki-queue.sh`
  (#73): state-dir paths read from `CCC_STATE_DIR`
  (default `/root/.claude/state`) instead of being literal-hardcoded.
  `distill/honcho-push.sh` also reads `CCC_HONCHO_CFG`
  (default `/root/.hermes/honcho.json`) for non-root / alternate-install
  scenarios. Matches the pattern already used by `load-memory.sh`'s
  `CCC_MEMORY_CACHE_DIR` / `CCC_HOOK_DIR`.
- `claude/settings.base.json`: new SessionStart hook entry that fires
  `queue-drain.sh` in the background (`& 2>/dev/null`) so it never adds
  latency to startup.
- `setup.sh`: copies the new `claude/hooks/distill/queue-drain.sh` alongside
  the other distill sub-scripts; chmod glob already covers it.

### Verified (dungae)
- Empty queue path: `queue-drain.sh` returns immediately, no log noise.
- Loaded queue path: seeded a synthetic failed payload, ran drain,
  Honcho POST returned HTTP 201, queue file truncated to 0 lines, message
  visible in Honcho with `metadata.replay: true` and the `(replayed)` content
  prefix. DELETE 202 cleanup of the smoke session.
- LIVE manual distill on the working session: attempt 1 succeeded in ~75 s,
  no retry needed, 2 honcho facts + 1 wiki candidate.
- Concurrent natural SessionEnd from another cwd's session distilled
  successfully alongside (4 candidates added) — single-flight lock didn't
  interfere.

### Notes
- The `_attempts` field is added in-band to the JSON line on each retry
  failure. Old queue lines (pre-0.3.18) without this field default to 0,
  so they retry up to `MAX_ATTEMPTS` total — graceful migration.
- No plugin.json bump (drain runs in node-local SessionStart, not in the
  portable plugin surface).

## [0.3.17] — 2026-06-22

Follow-up to 0.3.15 — harden `distill/extract.sh` against Haiku occasionally
returning prose instead of strict JSON (observed once on a natural `SessionEnd`
trigger fired from a code-debugging session — fail-open, but worth recovering).

### Fixed
- `claude/hooks/distill/extract.sh`:
  - Stronger user-prompt output contract: explicit "first non-whitespace is `{`,
    last non-whitespace is `}`, no prose, no preamble, no fences" plus an empty
    schema fallback for trivial sessions.
  - New `--append-system-prompt` constraint (belt + suspenders) attached to the
    `claude -p` invocation, restating the strict-JSON contract at system level.
  - **Two-attempt strategy**: if attempt 1's response fails `jq` validation, the
    script retries once with an even more emphatic system prompt (`CRITICAL
    OUTPUT CONTRACT...`) instead of failing immediately. Most Haiku "prose drift"
    cases recover on this single strict retry.
  - On final failure, the first 1 KB of each attempt's raw response is logged to
    `distill.log` for debugging.

Live verified on the dungae node: trigger=manual, attempt 1 produced valid JSON
(no retry needed), honcho POST returned HTTP 201 with 2 facts, 1 wiki candidate
queued, pipeline completed in ~80 s.

## [0.3.16] — 2026-06-22

Follow-up to 0.3.15 — add operator-facing `/distill` skill for manual control of the
Session Distiller pipeline introduced in the previous release.

### Added
- `claude/skills/distill/SKILL.md`: dispatches on the slash-command argument:
  - (empty) / `manual` — fire `distill.sh manual` and wait (polling `distill.log` up to
    180 s) before reporting what was distilled.
  - `status` — non-mutating: print toggle state, last `distill-last.json` summary,
    last 5 `distill.log` lines, and the wiki-candidates queue size.
  - `dryrun` / `live` — toggle DRY-RUN mode; uses `mv` (not `rm`) so the
    `guard.sh` `rm-catastrophic` rule never trips.
  - `disable` / `enable` — toggle the OFF switch the same way.
- The skill is picked up by the existing `setup.sh` skills `cp -r` line; no
  `setup.sh` change required.

## [0.3.15] — 2026-06-22

Session Distiller — `PreCompact`/`SessionEnd` hook pipeline that distills the live transcript
via `claude -p --model haiku` (inherits parent OAuth, no `ANTHROPIC_API_KEY` needed) and
routes the result to **Honcho** (auto push of working/relational facts) + a **human-gated
wiki-candidates queue** (`~/.claude/state/wiki-candidates.md`) for durable wiki promotion via
the existing `wiki-record` skill. Closes the gap left by the Hermes consolidator after the
ccc-node harness moved to Claude Code, without re-bloating `MEMORY.md`/`USER.md`.

Design rationale and live-check evidence: seoyoon-family-wiki `pages/team/dungae/DECISIONS.md`
**[TM-1058]**, log **[LOG-1212]** / **[LOG-1220]**. Runbook sections: `pages/nodes/dungae/RUNBOOK.md`
**[ND-1059]** (overview), **[ND-1060]** (troubleshooting), **[ND-1061]** (`rm-catastrophic`
guard-bypass pattern for LIVE flip).

### Added
- `claude/hooks/distill.sh`: entry hook. Recursion-guarded
  (`CLAUDE_DISTILL_INFLIGHT=1`), off-switch (`~/.claude/state/distill.disabled`), dry-run
  (`~/.claude/state/distill.dryrun`), min-content gate, backgrounded so the foreground
  hook returns instantly; resolves its sub-script directory dynamically so the same file
  works in both standalone (`~/.claude/hooks/distill.sh`) and plugin
  (`${CLAUDE_PLUGIN_ROOT}/hooks/distill.sh`) install modes.
- `claude/hooks/distill/extract.sh`: pulls the last N user/assistant turns from
  `~/.claude/projects/<cwd-encoded>/<session-uuid>.jsonl`, applies a secret-regex redact
  pass on top of `redact.sh` patterns, invokes `claude -p --model haiku
  --no-session-persistence --output-format text`, validates the strict-JSON response, and
  tags it with `session_id`/`trigger`/`distilled_at` metadata.
- `claude/hooks/distill/honcho-push.sh`: upserts the Honcho session and POSTs distilled
  working/relational facts to `{baseUrl}/v3/workspaces/<ws>/sessions/<sid>/messages` as
  `peer_id: <aiPeer>`. Fail-open with retry-queue stub (`honcho-queue.jsonl`).
- `claude/hooks/distill/wiki-queue.sh`: appends durable wiki candidates to
  `~/.claude/state/wiki-candidates.md` with title-hash 7-day de-dup; auto-bootstraps the
  queue header on first run. No auto-PR (human-gated per [FW-03]).
- `claude/settings.base.json`: registers `distill.sh` on `PreCompact` (after `checkpoint.sh`)
  and `SessionEnd` (after `notify.sh`) for both standalone and plugin install modes.
- `claude/hooks/enforcement-overlay.json` + `claude/hooks/hooks.json`: register
  `distill.sh` on `SessionEnd` only. PreCompact is handled exclusively by `settings.base.json`
  (merged into `~/.claude/settings.json` by `setup.sh`).
- `claude/settings.base.json` env: `CLAUDE_DISTILL_TIMEOUT="180"` — bigger budget than the
  90 s default for transcripts that exceed Haiku's first-token latency on large sessions.
- `setup.sh`: copies `claude/hooks/distill.sh` and the `claude/hooks/distill/` directory
  into `~/.claude/hooks/`, and `chmod +x` covers both directories.

### Changed
- `claude/hooks/load-memory.sh`, `claude/hooks/load-tools.sh`, `claude/hooks/checkpoint.sh`,
  `claude/hooks/refresh-memory.sh`, `claude/hooks/evidence-gate.sh`: each gains a single
  guard line right after `set -uo pipefail`:
  ```
  [ -n "${CLAUDE_DISTILL_INFLIGHT:-}" ] && exit 0
  ```
  This prevents the child `claude -p` session spawned by the distiller from re-firing
  memory loads / cache refreshes / checkpoints / Stop-time evidence checks.

### Verified
- Guards: all six hooks (`load-memory`, `load-tools`, `checkpoint`, `refresh-memory`,
  `evidence-gate`, `distill`) exit 0 silently under `CLAUDE_DISTILL_INFLIGHT=1`.
- Live Honcho POST: `ensure-session` returned HTTP 201, message POST returned HTTP 201,
  read-back confirmed peer/content/metadata round-trip, DELETE 202 cleanup.
- LIVE end-to-end manual run: `claude -p` Haiku call ~28 s, valid JSON parsed,
  2 wiki candidates auto-queued (later promoted to RUNBOOK [ND-1059..1061] in Wiki PR
  jinwon-int/seoyoon-family-wiki#1916).

## [0.3.14] — 2026-06-21

Bridge — extend `CCC_TELEGRAM_PART_HEADERS` to the entity-renderer path so multi-chunk
responses actually get a `k/N` marker under the default config (GitHub issue #34 follow-up).

### Fixed
- `bridge/core/streaming.py`: previously, `apply_part_headers` only ran on the MarkdownV2
  fallback path. With both `CCC_TELEGRAM_ENTITY_RENDERER` and `CCC_TELEGRAM_PART_HEADERS`
  default-on (slices 4 & 5), the entity path returned first and emitted multi-chunk
  responses with no part marker. The `PART_HEADER_RESERVE` headroom is now applied to the
  split limit for both renderers, and entity chunks pass through `apply_part_headers`
  before send.

### Added
- `bridge/utils/tg_entities.py`: `apply_part_headers(chunks)` — entity-path counterpart to
  `tg_readable.apply_part_headers`. Prepends `"k/N\n"` to each chunk text and emits a bold
  `MessageEntity` over the `k/N` digits (no `parse_mode` is set in the entity path, so
  asterisks would otherwise render as literal text). Existing entity offsets are shifted
  by the UTF-16 length of the prefix.
- `bridge/tests/test_tg_entities.py`: unit coverage for single/empty/multi-chunk behavior,
  offset shifting, and UTF-16-safe ASCII marker length.
- `bridge/tests/test_streaming.py`: integration test — a >TELEGRAM_LIMIT draft on the
  entity path lands as multiple bubbles, each starting with `k/N\n` and carrying a bold
  marker `MessageEntity`.

## [0.3.13] — 2026-06-21

Guard — narrow Telegram bridge restart carve-out for issue #34 slice 4 canary operations.

### Changed
- `claude/hooks/guard.sh`: allow the low-risk local `ccc-telegram-bridge` restart path
  (`.service` suffix optional) while preserving approval gates for broker/Gateway/A2A worker
  and other bridge service controls discovered during issue #34 canary operations.

### Added
- `claude/hooks/guard.test.sh`: acceptance coverage for allowed `ccc-telegram-bridge`
  restarts and denied A2A/worker/broker service controls.

## [0.3.12] — 2026-06-21

Fix — setup.sh did not install evidence-gate.sh (added in 0.3.8 but omitted from the
install list), so a real install referenced a Stop hook that wasn't on disk.

### Fixed
- `setup.sh`: copy `claude/hooks/evidence-gate.sh` into `~/.claude/hooks/` alongside the
  other portable hooks.
- `scripts/validate-harness.sh`: new check — every hook referenced by settings/overlay must
  also be installed by `setup.sh` (catches referenced-but-not-installed hooks at CI time).

## [0.3.11] — 2026-06-21

Permissions model — document the allow-all + fail-closed-hook decision (#13 item #3).

### Changed
- `claude/hooks/RISK-PROFILES.md`: add a "Permissions vs hook enforcement" decision
  section. Audit analysis (~1k tool calls) shows Bash usage is overwhelmingly
  compound/multi-line, which prefix-matched permission entries (`Bash(cmd:*)`) cannot
  describe — a per-command allowlist would over-block autonomous A2A/cron/headless runs.
  **Decision:** keep the broad `Bash(*)` allow and rely on `guard.sh` (regex, full-command,
  fail-closed) as the real Fresh-Approval enforcement; #13 item #3's "replace `Bash(*)`
  allow-all" is **superseded** for this node. No code/permission change — documents the
  existing, intentional model.

## [0.3.10] — 2026-06-21

Guard — relax the force-push gate for a developer's own feature branches (operator-approved).

### Changed
- `claude/hooks/guard.sh`: a *single explicit* `git push --force`/`-f`/`--force-with-lease`
  (or `+refspec`) to a **non-protected feature branch** now proceeds autonomously instead of
  being review-gated — it only rewrites that branch's own history, not shared/published state.
  The gate still **DENIES** (fail-closed) when the destination is a protected branch
  (`main`/`master`/`develop`/`release*`/`hotfix/*`/`prod`/`production`/`stable`), is
  ambiguous/bare (no explicit dst, `HEAD`, current branch), uses multiple refspecs, or is part
  of a compound/chained command. Destination is parsed from the command's positional args;
  when it can't be parsed unambiguously, the push is denied.
- `claude/hooks/RISK-PROFILES.md`: document the relaxation under `operator_review_gated`.

### Added
- `claude/hooks/guard.test.sh`: allow/deny cases for the relaxation (feature-branch allow;
  protected/ambiguous/multi/compound deny), and made the suite **hermetic** by stripping any
  ambient `CCC_ALLOW_GATED` (which would otherwise turn every gated case into a false "allow").

## [0.3.9] — 2026-06-21

Self-update skill — safe harness drift control (issue #13 Tier 2, item #16).

### Added
- `claude/skills/self-update/`: a skill that updates a node's installed harness
  (`~/.claude`) to ccc-node latest. `check.sh` is **read-only** drift detection
  (fetch + commits/files/CHANGELOG delta vs origin/main). Applying is **approval-gated**
  and routed through `setup.sh` (auto-snapshot to `~/.claude/backups/`), validated with
  `scripts/validate-harness.sh`, with an explicit rollback path. SKILL.md documents that
  node identity (CLAUDE.md, memories, honcho.json) is preserved by setup.sh's `seed()`
  and that the Telegram bridge is out of scope.

## [0.3.8] — 2026-06-21

Evidence gate — "evidence before declaring" Stop hook (issue #13 Tier 1.5, item #8).

### Added
- `claude/hooks/evidence-gate.sh`: opt-in (`CCC_EVIDENCE_GATE=1`) Stop hook. If the
  current session changed files (Write/Edit/MultiEdit/NotebookEdit) but the audit log
  shows no verification activity (tests / `--dry-run` / `--check` / `git diff`·`status` /
  CI checks), it blocks the stop **once** and asks for evidence. Loop-safe
  (`stop_hook_active` passes through), session-scoped, fail-open. Off by default.
- `claude/hooks/audit.sh`: record `session_id` so the gate can scope to the current
  session.
- Wired the gate into `Stop` in both `claude/hooks/hooks.json` and
  `claude/hooks/enforcement-overlay.json` (parity preserved); 6 new tests in
  `observability.test.sh` (23/23 pass).

## [0.3.7] — 2026-06-21

Harness settings — pin two operational `settings.json` keys (issue #13 Tier 3).

### Added
- `claude/settings.base.json`: `includeCoAuthoredBy: true` (keep the `Co-authored-by`
  trailer on Claude-made commits, matching the gh-pr-flow convention) and
  `cleanupPeriodDays: 30` (explicit chat/transcript retention period). First slice of
  the #13 harness-maturity roadmap's Tier 3 settings keys; the `model` pin is
  intentionally deferred (operational impact, decided separately).

## [0.3.6] — 2026-06-20

Telegram rendering — fix the MarkdownV2 path silently dropping long/symbol-dense messages
(and tables) to plain text. Follow-up to 0.3.4.

### Fixed
- MarkdownV2 escaping expands text ~1.2x (more for tables/symbol-dense content), so a
  sub-limit raw chunk could exceed Telegram's 4096-char limit once escaped and was dropped to
  **plain text — losing all formatting**. Both delivery paths now convert to MarkdownV2 **first**
  and split on entity-safe boundaries with `tg_md.split_markdownv2`, instead of splitting raw and
  hoping the escaped form fits.
  - `bridge/core/bot.py`: `_deliver_markdown` converts the whole message then splits the
    MarkdownV2 (removes the fragile raw-3500 headroom heuristic; per-part plain fallback only on
    the rare `BadRequest`).
  - `bridge/core/streaming.py`: `finalize_draft` upgrades the draft to the first MarkdownV2 chunk
    and emits the overflow as follow-up MarkdownV2 messages, instead of dropping the whole draft to
    plain when the escaped form exceeds the limit.
- `bridge/core/streaming.py`: `_find_split_boundary` no longer cuts through a fenced code block or
  a contiguous pipe table when overflowing between draft messages (new `_avoid_block_split` guard,
  floored at `max_length // 2`), so a table renders as one block instead of two broken halves.

### Changed
- `bridge/tests/test_streaming.py`: fixtures accept `parse_mode` (mirrors the real telegram Bot
  signature); added regression tests for overflow splitting and the block-boundary guard.

## [0.3.5] — 2026-06-20

### Fixed
- Telegram bridge no longer surfaces "❌ Internal error: Message is not modified..." to the
  chat. Telegram rejects no-op edits (identical text + reply markup) with a harmless 400; the
  streaming draft path already swallowed it, but inline-button / callback edit paths
  (`query.edit_message_text(...)`) did not, so the exception reached the global error handler and
  was posted to the user. `_error_handler` now detects this case and logs it quietly. New
  `bridge/utils/tg_errors.py` (`is_not_modified`) + `bridge/tests/test_tg_errors.py`.

## [0.3.4] — 2026-06-20

Telegram rendering — make tables and special characters display correctly instead of
falling back to plain text.

### Added
- `bridge/utils/tg_md.py`: renders standard Markdown to Telegram **MarkdownV2** via
  `telegramify-markdown` — GFM pipe tables become aligned fixed-width **code blocks** (a real
  table on mobile), and reserved special characters (`_ * [ ] ( ) ~ \` > # + - = | { } . !`) are
  escaped correctly so messages no longer hit `BadRequest` and drop to plain text. Decorative
  heading emojis are stripped (structure kept via bold). Degrades gracefully (returns `None`) when
  the library is absent so callers keep the legacy path. New dep: `telegramify-markdown>=0.5.0`.
- `bridge/tests/test_tg_md.py`.

### Changed
- `bridge/core/bot.py`: `_reply_smart` / `_send_smart` now route text through a shared
  `_deliver_markdown` helper that renders MarkdownV2 (per-chunk plain-text fallback on parse
  error). HTML callers (`/skills` listing) keep HTML; if telegramify is unavailable the legacy
  `wrap_markdown_tables` + Markdown path is used.
- `bridge/core/streaming.py`: `finalize_draft` upgrades the streamed message to MarkdownV2 on
  finalize (live drafts stay plain), so streamed responses also render tables/formatting. Any
  parse/length edge case falls back to the original plain text — delivery is never lost.

## [0.3.3] — 2026-06-20

Node onboarding hardening — closes the P2–P4 gaps found bringing up `soonwook`/vps6 standalone
(issue #25). P1 shipped in #24, P5 in #27.

### Added
- **P2 — Linux reboot-persistence for the Telegram bridge.** `bridge/start.sh` gains
  `--install-systemd` / `--uninstall-systemd`. Run as root it writes a system unit to
  `/etc/systemd/system/ccc-telegram-bridge.service` and `systemctl enable --now`s it; run as a
  normal user it installs a `systemctl --user` unit. The unit runs the bridge in the foreground
  under systemd supervision (`Restart=on-failure`); name overridable via `BRIDGE_SERVICE_NAME`.
  No more hand-written units (cf. the manual `ccc-telegram-bridge.service` on soonwook).
- **P4 — node-identity seeding.** `setup.sh` accepts `--node`, `--display`, `--slot`,
  `--fleet-role`, `--lang`, `--user-name`, `--user-gh`, `--user-tz`, `--user-context` and
  substitutes the matching `<PLACEHOLDER>` tokens in freshly-seeded `CLAUDE.md` / `MEMORY.md` /
  `USER.md`. Omitted tokens are left intact for manual editing; existing files are never rewritten.

### Changed
- **P3 — `setup.sh` no longer overwrites `~/.claude` without a restore point.** Before clobbering
  `settings.json`, `settings.local.json`, and the hook/output-style/agent/command/skill dirs it
  tars them to `~/.claude/backups/ccc-node-setup-<ts>.tar.gz` (credentials excluded). Skip with
  `--no-backup`.
- `bridge/README.md`: documents Linux systemd install and lists Linux under Platform Support.

## [0.3.2] — 2026-06-20

A2A Claude Code worker lane docs — capture the `soonwook` follow-up conversion and remove a
few `nosuk`-only labels from portable harness messages.

### Added
- `docs/a2a-claude-worker.md`: documents the poller-service vs analysis-backend split for A2A
  lanes where `a2a-hermes-worker` remains the systemd poller name but `OPENCLAW_BIN` /
  `A2A_OPENCLAW_ANALYSIS_BIN` point at `claude-a2a-analysis-bridge.mjs` and broker metadata
  reports `runtime=claude-code`.
- `/a2a-claim` and `CLAUDE.md.template` now explicitly warn workers to classify A2A runtime from
  live env + broker metadata instead of service name.

### Changed
- Session memory status messages and injected heading are now node-generic (`CCC_NODE`,
  `/root/.claude/state/node.txt`, or hostname) instead of hard-coded `nosuk`.

## [0.3.1] — 2026-06-20

Plugin-mode install — resolve portable-hook double-firing between `setup.sh` and the plugin.

### Changed
- **Single owner per portable hook.** `settings.json` and an enabled plugin both register hooks and Claude Code does not de-duplicate them, so a node running both would fire guard/audit/redact/notify **twice**. `setup.sh` now composes `settings.json` from two sources and you pick one owner per node:
  - `claude/settings.base.json` — node-local hooks (SessionStart/Pre+PostCompact) + statusLine + outputStyle, always installed.
  - `claude/hooks/enforcement-overlay.json` — the portable hooks (guard/audit/redact/notify), absolute paths.
  - `./setup.sh` (standalone, default): merges base + overlay → `settings.json` owns everything.
  - `./setup.sh --with-plugin`: installs lean settings (base only); the **plugin** owns the portable hooks.
- The static `claude/settings.json` is removed; it is now generated at install (single source of truth, no drift).
- Validator: base/overlay hook events must be disjoint; the overlay must stay equivalent to the plugin's `hooks/hooks.json` (same events/matchers/script basenames, modulo the `${CLAUDE_PLUGIN_ROOT}` vs `/root/.claude` path prefix) so both modes enforce identically; the rendered standalone settings is validated.

### Notes
- Reference/bootstrap nodes (e.g. nosuk) stay **standalone**; plugin mode is for nodes that consume ccc-node via the marketplace. Mixing modes on one node is what double-fires — don't enable the plugin on a standalone install.

## [0.3.0] — 2026-06-20

Tier 1.5 follow-up — Telegram push delivery (token-isolated, owner-only, opt-in).

### Added
- **Bridge push notifier** (`bridge/core/push_notifier.py`): a background task in the Telegram bridge that delivers Claude Code lifecycle notifications to the **owner only**, decoupled from the hook via a filesystem spool. The bot **token never leaves the bridge** — the hook writes summaries, the bridge sends them. Owner target = `CCC_PUSH_CHAT_ID` or the sole `ALLOWED_USER_IDS`. Rate-limited, deduplicated, best-effort (delivery failure never crashes the bot). **Disabled by default** (`CCC_PUSH_ENABLED`); merging/restarting is a no-op until an operator opts in.
- **notify hook spool** (`claude/hooks/notify.sh`): with `CCC_NOTIFY_TELEGRAM=1`, Notification/Stop events also write a short, **redacted** summary (token-shaped runs masked, length-capped) into `~/.claude/state/telegram-spool`. Off by default; never touches the bot token.
- Config (`bridge/utils/config.py`): `CCC_PUSH_ENABLED` / `CCC_PUSH_CHAT_ID` / `CCC_PUSH_SPOOL` / `CCC_PUSH_POLL_INTERVAL` / `CCC_PUSH_MAX_PER_MINUTE`, documented in `bridge/.env.example`.
- Tests: `bridge/tests/test_push_notifier.py` (11 cases — disabled-by-default, owner target resolution, dedup, rate-limit, retry-on-failure, malformed-archive, format); observability suite +4 (spool off-by-default, opt-in, redaction, node label) → 17/17.

### Approval boundary
- Telegram delivery is an outbound provider send. It stays **opt-in + owner-only + token-isolated** by construction; first live activation (set `CCC_PUSH_*`, restart the bridge) remains a separate, explicitly-approved step.

## [0.2.0] — 2026-06-20

Tier 3 — presentation & headless surface.

### Added
- **Status line** (`claude/hooks/statusline.sh`): one-line at-a-glance bar — node · model · git branch+dirty · context % (color-coded: green/amber/red) · `⚠200k` token warning · session cost · A2A task marker · active output style. Reads Claude Code's stdin session JSON; degrades gracefully on empty/garbage input. Wired via the node-local `settings.json` `statusLine` field (the main status line is not applied from a plugin's `settings.json`).
- **Output style** (`claude/output-styles/ccc-report.md`): Korean structured-reporting default — 확정/변경/리스크/다음 절 구분, 진행 내레이션(짧은 분리 메시지), 번호형 질문, Fresh-Approval 경계, secret 비노출. `keep-coding-instructions: true` (coding behaviour unchanged). Ships via the plugin's `output-styles/` and is activated through `settings.json` `outputStyle: "ccc-report"`.
- **Headless runner** (`claude/headless.sh`): `claude -p` wrapper for cron / A2A / CI — JSON output, read-only tool baseline (override via `CCC_ALLOWED_TOOLS`/`CCC_PERMISSION_MODE`), stdin piping, session+cost logging. Guard enforcement still applies (non-`--bare`).
- Validator coverage: statusline smoke (sample + empty input), `settings.json` statusLine/outputStyle wiring resolves to shipped files, output-style frontmatter, headless/statusline `bash -n` + shellcheck.

### Notes
- Plugin `details` inventory does not list output styles as a category, but `output-styles/ccc-report.md` ships at the plugin root and loads when the plugin is enabled (verified by isolated install).

## [0.1.1] — 2026-06-20

### Fixed
- **Plugin now actually loads.** The 0.1.0 manifest passed `claude plugin validate` but failed at install time (`Status: ✘ failed to load`). Two distinct defects, both confirmed by a real isolated install on Claude Code 2.1.183:
  1. `plugin.json` referenced `./hooks/hooks.json` in its `hooks` field, but `hooks/hooks.json` is auto-loaded — the duplicate reference aborted the whole plugin load.
  2. `agents`/`commands` custom-path **arrays** (`./claude/...`) are schema-valid but silently load **0** components in this CLI; only default-location discovery is honoured.
- **Fix**: the marketplace entry now points the plugin root at the existing component tree via `source: "./claude"`. The manifest moves to `claude/.claude-plugin/plugin.json` with **no path fields** (agents/commands/skills/hooks auto-discovered), and the hook config moves to `claude/hooks/hooks.json` with `${CLAUDE_PLUGIN_ROOT}/hooks/*.sh` paths. No `claude/` restructure; `setup.sh` is unaffected.
- Verified real install: `Status: ✔ enabled` — Skills 7 (incl. 3 commands), Agents 4, Hooks 6, all hook scripts resolve.
- **Validator hardened** (`scripts/validate-harness.sh`): now resolves every `${CLAUDE_PLUGIN_ROOT}` hook path to an on-disk script, rejects silent-load path fields, asserts `source: "./claude"`, and runs `claude plugin validate` when the CLI is present — the checks that would have caught 0.1.0.

## [0.1.0] — 2026-06-20

First versioned/packaged release. Installable as a Claude Code **plugin** (`/plugin marketplace add jinwon-int/ccc-node` → `/plugin install ccc-node@ccc-node`) in addition to the existing `setup.sh` bootstrap.

### Added
- **Plugin packaging**: `.claude-plugin/plugin.json` manifest + `.claude-plugin/marketplace.json` catalog + `hooks/hooks.json` (enforcement + observability hooks via `${CLAUDE_PLUGIN_ROOT}`). Packages the node-agnostic surface (skills, slash commands, A2A agents, guard/audit/redact/notify hooks).
- **Tier 1 enforcement** — `guard.sh` PreToolUse fail-closed guard for the Fresh-Approval set, with `CCC_ALLOW_GATED=1` operator escape hatch; risk-profile mapping (`RISK-PROFILES.md`); `permissions.deny`/`ask`.
- **Tier 1.5 observability** — `audit.sh` (PostToolUse, secret-redacted JSONL), `redact.sh` (UserPromptSubmit secret-awareness), `notify.sh` (Notification/Stop/SessionEnd; approval-needed log + working-state archive).
- **Tier 2** — harness CI (`scripts/validate-harness.sh` + `.github/workflows/ci.yml`); slash commands `/node-status`, `/a2a-claim`, `/wiki-log`.
- Skills: `wiki-record`, `mcp-add`, `skill-suggest`, `gh-pr-flow`.
- A2A worker sub-agent roster: `a2a-explorer`, `a2a-researcher`, `a2a-implementer`, `a2a-verifier`.

### Notes
- **Node-local memory bootstrap** (SessionStart/PostCompact memory injection, working-state checkpoint) stays in `setup.sh` — it is inherently node-specific and not part of the portable plugin.
- Two install paths coexist: plugin (portable surface) + `setup.sh` (memory bootstrap + node templates).
