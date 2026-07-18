# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- **Telegram bridge memory could cross from DMs into public chats.** The new
  opt-in `CCC_BRIDGE_MEMORY_MODE=audience-scoped` routes public conversations
  to one shared local store and each DM to an opaque HMAC-derived private store;
  DMs can recall shared facts, while groups/channels cannot read DM-private or
  unscoped legacy sources. Resume/checkpoint/distill queues, local facts, caches,
  and indexes inherit the same paths, shared facts carry an explicit privacy
  label, and raw Telegram ids never enter memory paths or hook settings. Global
  Honcho and Family Wiki read/write paths are disabled in this mode until they
  have physical audience sessions/labels. Memory plus `shared-all` now fails closed unless
  an explicit unsafe override restores legacy `curated` behavior.
- **Outbound file delivery covered too few file types.** Replies that referenced
  an agent-produced file only auto-sent it to Telegram when its extension was one
  of nine hardcoded types (`png/jpg/jpeg/gif/webp/mp4/mp3/pdf/zip`), so common
  deliverables like `.csv`, `.md`, `.txt`, `.json`, `.xlsx`, `.docx`, `.pptx`,
  archives, and most audio/video were silently never sent. `_FILE_PATH_RE` is now
  built from a curated `_SENDABLE_FILE_EXTENSIONS` list spanning document, data,
  spreadsheet, presentation, archive, and audio/video/image families (source code
  and executables stay excluded so ordinary coding turns don't push every edited
  file), with an order-independent trailing boundary that prevents partial-suffix
  clipping (e.g. `.json` → `.js`). The auto-send size ceiling is raised from 10 MB
  to the Telegram Bot API's 50 MB document limit (`MAX_SEND_FILE_BYTES`).
- **Files outside `PROJECT_ROOT` were silently dropped on send.** The referenced
  file confirmation flow (`_prompt_outside_file_confirmation` / the `extsend:`
  callback) existed but was never wired into the reply path, so any deliverable
  the agent produced outside the project root simply never arrived. Text,
  voice-only artifact, and send paths now route out-of-project files through
  `_maybe_prompt_outside_files`,
  which offers a one-tap confirm before sending (and is skipped when the owner
  user id is unknown, so callers without one cannot expose out-of-project paths).
  In-root files continue to send automatically.

### Changed
- **Accept all inbound Telegram document types (follow-up to #503/#505).** The
  inbound document path no longer filters by file type: the MIME/extension
  allowlist preflight and the post-download executable-magic-byte rejection are
  removed, so every file an allowlisted user sends is downloaded and forwarded
  to the active agent. The Telegram allowlist remains the trust boundary; all of
  #505's storage hardening is retained (validated owner-owned `0700` dirfd,
  `O_EXCL`/`O_NOFOLLOW`, random server-side names, validated regular-file `0600`
  permissions, bounded writes, metadata/size rechecks), the bridge never
  executes uploads, and agent-side execution stays gated by the Bash tool
  policy. The stored artifact now preserves the sender's real suffix when it is a
  safe token (falling back to a MIME-derived extension, then `.dat`). Size
  enforcement via `CCC_MAX_DOCUMENT_SIZE_MB` is unchanged.
- **Provider-aware bridge health (#481).** Runtime readiness now probes the
  configured agent provider: Claude nodes use `claude auth status --json`,
  while Codex nodes use `codex login status`. `health.json` publishes the
  active provider under `agent`, and `start.sh --status` labels that component
  as Claude or Codex while retaining the legacy `claude` field for schema-v1
  consumers.
- **Unrestricted Codex package default (#415).** The default `auto-approve`
  policy now sends `approvalPolicy=never`, no reviewer, and
  `sandboxPolicy={type: dangerFullAccess}` on every Codex turn regardless of
  execution profile. This supersedes the network-off workspace default added
  in #412: root bridges can now access external/Tailscale networks, host files,
  systemd, SSH, devices, and paths outside the workspace without prompting.
  Every allowlisted Telegram user is part of the effective trust boundary;
  single-owner operation is strongly recommended. Explicit `auto-review`,
  `approve-each`, and `disabled` policies retain their prior behavior.

### Added
- **Provider-neutral read-only `/usage` (#502).** Authorized Telegram users can
  inspect Codex account rate limits/token summaries and exact-thread token
  updates through the official app-server protocol without starting a turn.
  Claude sessions expose already-observed Agent SDK usage/cost plus optional
  documented status-line context and Pro/Max windows via a TTL-bound,
  owner-only, atomic allowlist snapshot. Missing fields remain explicitly
  unavailable; transcripts, credentials, private endpoints, and reset-credit
  consumption are never used.
- **Private inbound Telegram documents (#503).** Non-image `Document` updates now
  pass the allowlist before download, land under the project-scoped bot data
  directory with collision-resistant names, validated dirfd-relative `O_EXCL`/
  `O_NOFOLLOW` creation, and exact `0700`/`0600` permissions. Declared Telegram
  metadata and bounded actual writes are both enforced via
  `CCC_MAX_DOCUMENT_SIZE_MB`; MIME/extension mismatches and executable magic are
  rejected, while queue overflow and download/runtime failures are user-visible
  instead of silently dropped. Sensitive document prompts bypass normal
  chat-input logging in favor of a fixed audit event.
  Temporary files are removed on success, failure, cancellation, and
  stale-startup cleanup.
- **Coverage, mypy, and complexity regression gates (#348).** The CI coverage
  run now measures branch coverage and fails under 72% (measured baseline
  74.1%; staged plan to 80% in `docs/quality-baseline.md`). Ruff gains the
  `C901` complexity gate (`max-complexity = 15`): new functions above CC 15
  fail CI, while the 19 pre-existing hotspots carry explicit per-function
  `# noqa: C901 -- #348 baseline hotspot` markers so the exception list stays
  review-visible. Mypy now checks all 34 `bridge/core` modules — six real
  errors surfaced by the expansion were fixed (lazy-init collaborator
  annotations in `bot_voice`, an `Optional` declaration for
  `_fatal_polling_error` in `bot_lifecycle`, a stale ignore in
  `session_isolation`) — with a single review-visible baseline override
  (`attr-defined` only, mixin modules only) pending the typed-composition
  refactor. New behavior tests cover the deny/error/empty/no-match paths of
  the access and delivery mixins (allowlist denials per update type,
  stale-message drops, Bash per-call approval round-trip incl. digest
  mismatch, outside-path approval round-trip, resume-selection
  mismatch/no-match, pending-question consumption, queue overflow, heartbeat
  status callback send/edit/delete/error fail-open contract).
- **Hash-locked runtime dependency installs + wheel smoke gate (#349).**
  `bridge/requirements.lock.txt` is a new pip-compile `--generate-hashes` lock
  of the runtime dependency set, generated from the same source as the CI lock
  (`bridge/pyproject.toml`) and constrained to it so runtime nodes install
  exactly the versions CI tested. `start.sh` now installs it with
  `pip --require-hashes` by default and adds the first-party bridge package
  with `--no-deps`, so clean installs at the same checkout resolve identical
  versions/hashes and unhashed transitive dependencies are rejected; the
  legacy lower-bound `requirements.txt` flow remains available behind the
  documented `CCC_DEPS_UNLOCKED=1` escape hatch — honored from the process
  environment or, like other bridge settings, from the project/global `.env`
  (the locked path also drops
  the previous unpinned `pip install --upgrade pip`, removing an
  install-time nondeterminism source). Both locks are regenerated together by
  the new `scripts/ccc-deps-lock.sh`, which records the Termux/Linux/macOS
  single-lock marker policy and enforces runtime⊆CI version consistency —
  also guarded in CI by the new `tests/test_runtime_deps_lock.py`. A new
  `wheel-smoke` CI job (declared as a required context in
  `.github/required-checks.json`; the live branch-protection addition follows
  the approved #350 post-merge pattern) builds the bridge wheel with
  `--no-isolation` (the setuptools backend is hash-pinned in the CI lock, so
  the build fetches nothing unhashed), installs the hash-locked runtime set
  plus the wheel with `--no-deps` into a clean venv,
  import/config-smokes the installed package outside the source tree, and
  makes `pip check` and `pip-audit` (over the hash-locked runtime set)
  blocking gates.
- **Runtime health probe + threshold alerts, detection-only (#389).** A new
  periodic lifecycle task exports four structured signals to
  `health.json → signals` every `CCC_HEALTH_ALERTS_INTERVAL_SECONDS` (60):
  session liveness (registered streams whose reader task died), heartbeat age
  vs request lifetime (oldest in-flight request compared to
  `CLAUDE_PROCESS_TIMEOUT` — the #307 "outlived its own lifetime" class),
  pending/dropped notifications (push-spool backlog + cumulative quarantined
  transcripts from #411 B), and orphan node-claude children (read-only #303
  probe). Configurable thresholds fire redaction-safe alerts (constant
  templates + counts only — never tokens, prompts, or paths) that are logged,
  counted in `health.json`, and queued through the owner-only push-notifier
  spool with per-code cooldown — so a real Telegram send additionally requires
  the existing `CCC_PUSH_ENABLED` opt-in and inherits its dedup and rate
  limits. Detection-only by design: no restart, reap, or any remediation.
  Thresholds and their rationale are documented in `.env.example`.
- **End-to-end delivery reliability harness (#385).** `tests/test_e2e_delivery.py`
  boots the real bridge composition — `TelegramBot` → `ProjectChatHandler` with
  the real reader loop, typing keepalive, session store, task ledger, and
  dead-session recovery — over a fake Telegram surface and a scripted Claude
  SDK client (no network, no live provider, no real token). Three round-trips
  execute for real: a solicited user message delivering its reply to the
  Telegram outbound exactly once; an unsolicited background wakeup turn with no
  pending request still delivering exactly once (#364 P1 — the reader used to
  drop these entirely); and a dead session's persisted terminal task
  notification delivered by the recovery pass and deduplicated on the next pass
  (#364 P2 / #372). A negative control re-introduces the pre-#371 reader drop
  behavior and shows the unsolicited round-trip then fails, proving the
  positive test is the tripwire for that regression class. Runs
  deterministically inside the required `bridge-tests` CI gate.
- **Explicit safe Codex default (#412).** The default `auto-approve` policy now
  sends `approvalPolicy=never`, no reviewer, and a network-off `workspaceWrite`
  sandbox on every Codex turn. New clean Codex deployments therefore no longer
  depend on a node-local `config.toml` sandbox default to preserve the intended
  low-friction workspace boundary; explicit node policy overrides remain intact.
- **Codex low-friction auto-review policy (#409).** `CCC_BRIDGE_BASH_POLICY=auto-review`
  now sends the deployed app-server's exact `on-request` approval policy,
  `auto_review` reviewer, and a network-off `workspaceWrite` sandbox on every
  turn. Routine workspace work continues automatically while eligible boundary
  crossings are evaluated by Codex's reviewer agent. The policy is transported
  through the provider-neutral session contract, participates in session-cache
  invalidation, never enables `dangerFullAccess`, and degrades conservatively to
  per-call human approval when the active provider is Claude.
- **Telegram reply-context injection** (`core/bot_shared.build_reply_context_prefix`, #380).
  When a user *replies* to an earlier message, the bridge previously forwarded
  only the new text and dropped the quoted original, so the agent couldn't tell
  which prior message was referenced. Now the replied-to original is prepended as
  a single-line, JSON-encoded context record before the user's text, across the
  text, photo, and voice handlers. The extraction follows the Hermes Agent
  pattern: prefer Telegram's native partial quote (`message.quote`) so replying
  to one selected substring of a multi-section message doesn't inject the whole
  original; otherwise fall back to the replied-to text/caption and cap the
  snippet at 500 chars. Exact bot and owner identities are distinguished;
  different or unknown authors are labeled as untrusted context that is never
  instructions, and quote/newline/control characters cannot escape the record.
  Non-reply messages and text-less media replies are unchanged. 18 helper tests
  plus 3 text/photo/voice wiring tests cover the trust boundary.
- **Persistent task ledger** (`core/task_ledger.py`) — the structural fix for the
  recurring "typing / ⏳ Working stuck after the work is done" class of bugs, ported
  from the Hermes/A2A task-lifecycle model (jinwon-int/a2a-nexus `task-projection.ts`,
  terminal-outbox pattern). Previously the bridge *inferred* request status from
  in-memory stream liveness, so every crash/hang/race left indicators stranded.
  Now every request gets a persisted record with an explicit state
  (`working` / `input-required` → `completed` / `failed` / `canceled` / `timeout` /
  `interrupted`) and the status message is a projection of it:
  - every completion/cancel/timeout/error path is a terminal transition through the
    ledger — an indicator can no longer outlive its task record;
  - a terminal cleanup whose Telegram delete fails leaves a retryable `terminal_op`
    (mini terminal-outbox), drained every 10s and at startup;
  - startup reconciliation marks records from a dead process `interrupted` and edits
    their frozen "⏳ Working" message into a short resend notice
    (`CCC_TASK_INTERRUPTED_NOTICE=false` to delete instead); ledger path override:
    `CCC_TASK_LEDGER_PATH` (default `BOT_DATA_DIR/tasks.json`).
  - 14 new unit/integration tests (`test_task_ledger.py` + heartbeat-loop ledger cases).

### Changed
- **Termux session-store path compatibility.** Ancestor validation now permits the
  current Termux app's exact non-world-writable `.../com.termux/files` root and
  OS-owned Android platform ancestors on validated, current-UID-owned canonical
  Termux data paths. Symlinks, untrusted group/world-writable ancestors,
  missing or spoofed prefixes, and writable final storage directories remain
  fail-closed.
- **Execution profiles (#376).** `CCC_BRIDGE_EXECUTION_PROFILE` now separates the
  SDK execution boundary from `CCC_BRIDGE_BASH_POLICY` approval UX. The package
  default `strict-project` preserves PR #363's fail-closed project-root OS
  sandbox. Explicit `owner-operator` restores normal host-capable Claude Code
  execution and user/project/local settings only when
  `CCC_REQUIRE_ALLOWLIST=true` and `ALLOWED_USER_IDS` resolves to one owner;
  shared/open owner mode refuses startup. `disabled` and unknown/unsafe values
  hard-deny Bash and suppress filesystem settings sources so settings hooks
  cannot retain host execution. Startup logs expose the effective profile, Bash
  policy, and host-scope boolean without logging owner IDs or secrets.
- `CCC_BRIDGE_BASH_POLICY` has three explicit states and defaults to
  `auto-approve`. Bash now runs inside a strict Claude Code OS sandbox with
  unsandboxed fallback/excluded commands disabled, host reads denied by default,
  and SDK startup failing closed when the sandbox is unavailable. The bridge
  ignores user/project/local settings for SDK streams so merged filesystem
  arrays cannot widen the boundary. `approve-each` adds Telegram confirmation
  on top of the same sandbox; `disabled` removes Bash entirely. Unknown values
  fail closed. Linux/WSL2 requires `bubblewrap` and `socat`.

### Fixed
- **Requests whose terminal event never arrives are released within a bounded
  grace instead of blocking the conversation for hours (#411, part C).** When
  the agent produced answer text but the terminal event (Claude
  `ResultMessage` / provider completion) vanished, the request previously
  stayed `working` until the full process timeout (default 21600s), and
  same-conversation serialization blocked every follow-up message with it
  (observed live: a 1,866-char answer written to the transcript, never
  delivered, FIFO stuck for 26+ minutes while all health probes read
  healthy). Both provider paths now share one lifecycle invariant, tuned by
  `CCC_TERMINAL_STALL_SECONDS` (default 300, 0 disables): once answer text is
  the latest meaningful activity — no tool running, no approval pending — and
  the stream stays silent for the grace period, the buffered answer is
  delivered exactly once with an explicit stall notice, the turn is
  interrupted, the task-ledger record terminalizes, and the conversation FIFO
  releases so queued messages proceed. Exactly-once is structural: the Claude
  path tears down the dead stream and swallows at most one late
  `ResultMessage` racing the teardown; the provider-neutral path closes the
  abandoned event iterator so a late completion has no consumer. Long tool
  runs and input-required approval waits are exempt (their silence is
  legitimate), and a fresh stream event restarts the countdown. Released
  requests are counted in `health.json → requests.stalled`.
- **Rejected dead-session transcripts are quarantined instead of rescanned
  forever (#411, part B).** When dead-session recovery rejects a transcript as
  unsafe to replay (`TranscriptRejected`), the bridge previously re-parsed and
  re-warned about the same immutable file on every 60-second tick, with no
  owner notification (observed live: identical rejection warnings every minute
  for over an hour). A rejection now persists a quarantine record in the
  conversation's session state, fingerprinted by session id, constant reason
  code, and the file's dev/inode/size/mtime identity. Subsequent ticks skip the
  parse entirely while the identity is unchanged; the owner gets exactly one
  redacted notice (reason code only — never transcript content or paths)
  explaining that automatic recovery is impossible and the task should be
  re-run if still needed. A failed notice is retried on later ticks without
  re-parsing. Identity drift (operator touch/replace) or session rotation
  triggers a bounded re-evaluation: a repaired transcript resumes normal
  recovery and lifts the quarantine; a still-broken one re-quarantines under
  its new fingerprint. Quarantine state survives restarts (it lives in
  `sessions.json`), retention is one record per conversation by construction,
  and new quarantine events are counted in
  `health.json → recovery.quarantined_transcripts`.
- **Transient Telegram outages no longer cancel in-flight AI turns (#411, part A).**
  A runtime `NetworkError`/`TimedOut` or a watchdog-detected hang previously tore
  down the whole Application lifecycle, taking the in-progress agent turn with it
  (observed live: a ~10s transport blip cancelled a running Claude turn and its
  answer was never delivered). Polling exits are now supervised: the bridge first
  runs a bounded transport-only reconnect (`_RECONNECT_ATTEMPTS`, exponential
  backoff) that restarts only the updater — the Application object, bot request
  pools, conversation FIFO, and every in-flight agent turn survive untouched, so
  a turn finishing mid-outage still delivers exactly once through the surviving
  bot. Escalation to the full teardown/rebuild path happens only when the
  reconnect fails outright or reconnected polling keeps dying within
  `_MIN_UPTIME` (preserving the rapid-crash SystemExit accounting). The polling
  watchdog now stays alive after triggering a restart, since no rebuild recreates
  it after a transport-only reconnect. Reconnects never drop pending updates —
  only the process's very first polling start drops the backlog — so messages
  sent during an outage are no longer lost. Permanent failures (invalid token,
  getUpdates conflict, revoked token) keep their fail-closed SystemExit, and the
  in-flight requests they terminate are now attributed in
  `health.json → transport.cancelled_by_transport`; successful transport-only
  reconnects increment `transport.reconnects`. Both `start_polling` calls now
  register a synchronous `error_callback`: python-telegram-bot retries
  getUpdates errors indefinitely in a background loop with `updater.running`
  still True, so a `Conflict`/`Forbidden` raised *after* polling started would
  otherwise never surface anywhere — the callback flags permanent errors and
  the polling supervisor re-raises them into the fail-closed handlers.
  `InvalidToken` takes a third path: PTB re-raises it inside the retry loop
  *without* invoking the error callback, killing the polling task while
  `updater.running` stays True. The supervisor now also watches the polling
  task itself (defensively, with the get_me watchdog as fallback) and routes
  a task killed by a permanent error into the same fail-closed handlers.
- **Canonical update provenance (#351).** `bridge/start.sh` no longer compares
  the vendored bridge changelog with `terranc/claude-telegram-bot-bridge`
  releases and then pulls whichever checkout happens to be current. `--upgrade`
  now delegates to the hardened `scripts/ccc-self-update.sh` against the
  canonical repository and pinned `main` branch, preserving its exit code;
  `--version`, startup output, doctor, and fleet reports share the
  `scripts/ccc-version.sh` checkout identity. Canonical clone/update docs and a
  fixture-based no-network regression test cover the compatibility entry point.
- **Heartbeat cleanup retry path (#307).** If deleting a stalled `⏳ Working` status
  message fails, the bridge now keeps the message id on the live request so the
  next heartbeat tick retries the cleanup instead of losing the id and leaving a
  dangling status line behind.
- **Self-update restarted the bridge mid-task**, killing in-flight `claude` children
  (SIGTERM → exit 143) and destroying the user's work — the root cause behind the
  frozen heartbeats below and the frequent "resend please" interruptions. The bridge
  now publishes an in-flight `workload` snapshot (active request count + oldest age)
  to `health.json` every 10s, and `ccc-self-update.sh` **defers the whole run (exit 8)
  while the bridge is busy**, retrying on the next scheduled tick. Bounded so it can't
  starve updates (`CCC_SELF_UPDATE_BUSY_MAX_SECONDS` per task, `..._MAX_DEFER_SECONDS`
  total); fail-open and `--force`-bypassable. See `docs/self-update.md`.
- **Dangling `⏳ Working — Nm` heartbeat** left frozen as the last chat message after a
  task was effectively done. Two independent causes, both fixed:
  - **Restart orphans (primary).** When the bridge is SIGTERM-killed mid-request
    (exit 143 — frequent on Android/Termux), the in-flight request dies with the
    process and its heartbeat message is never deleted; the restarted bridge has no
    in-memory record of it. Heartbeat message ids are now persisted to
    `BOT_DATA_DIR/heartbeats.json` (`utils/heartbeat_store.py`) on creation and
    discarded on clean deletion, and the bridge sweeps any survivors on startup
    (`_on_ready`, alongside the orphan-process reaper) — mirroring the process reaper
    but for Telegram messages. Override the path with `CCC_HEARTBEAT_STORE_PATH`.
  - **Live stalls (secondary).** If a still-in-flight request goes silent without
    reaching its terminal `ResultMessage` (hung stream), the heartbeat used to tick up
    until the 6-hour `CLAUDE_PROCESS_TIMEOUT`. The reader loop now stamps
    `_PendingRequest.last_event_at` on every SDK event, and `_maybe_update_heartbeat`
    deletes the heartbeat once the stream has been silent for
    `CCC_HEARTBEAT_STALL_SECONDS` (default 300 s; 0 disables). It reappears if activity
    resumes. A legitimately long single tool call emits no intermediate events, so
    raise this value if you run such tools.
  - 12 new unit tests: 9 for the heartbeat-id store, 3 for stall deletion.
- **Orphaned `node claude` processes** accumulate on Android/Termux when the bridge
  restarts or crashes (jinwon-int/ccc-node#303). Root causes addressed:
  - Added `utils/orphan_reaper.py`: scans `/proc` for PPID=1 `node claude` processes
    older than 30 min and SIGTERMs them. No psutil dependency; works on Linux/Termux.
  - Bridge now sweeps orphans at **startup** (`_on_ready`) to clean up survivors from
    a previous crashed run.
  - A **periodic reaper asyncio task** sweeps every 15 minutes during normal operation.
  - Increased `client.disconnect()` timeout from 3 s → 15 s so the SDK transport has
    enough headroom (5 s EOF wait + 5 s SIGTERM + buffer) to actually kill the subprocess
    before the bridge gives up and potentially orphans it.
- 26 new unit tests cover the reaper utility end-to-end.

## [0.10.1] - 2026-05-31

### Fixed
- Skip empty proxy env vars to avoid httpx parse error

## [0.10.0] - 2026-05-25

### Changed
- Migrate from `claude-code-sdk` to `claude-agent-sdk` (v0.1.72+) for improved Claude API integration
- Add OpenTelemetry dependencies for enhanced observability support

### Added
- System prompt instructions for automatic image/file path detection and delivery
- Application screenshots to README showcasing streaming response, voice message, and code editing features

### Fixed
- Auto-split `/skills` command responses exceeding Telegram's 4096 character limit using `_reply_smart`
- Normalize model name `[1M]` suffix to prevent duplicate suffixes (e.g., `[1M][1m]`)
- Add debug logging for file artifact sending to improve troubleshooting

### Removed
- Redundant proxy environment variable passthrough in daemon supervisor (environment inherits from parent)

## [0.9.5] - 2026-04-10

### Added
- Pass proxy environment variables (`http_proxy`, `https_proxy`, `all_proxy`, `no_proxy`) to launchd plist so bot can connect through proxy in startup service mode
- Wait for bot process to start (up to 5 seconds) after `--install` to ensure `--status` returns valid state immediately

### Fixed
- In launchd mode, mark service state as `starting` instead of `unavailable` during restart windows so status reflects that launchd will respawn the process
- Stop bot process before uninstalling launchd plist to ensure clean removal

### Changed
- Document macOS Full Disk Access requirement for `~/Documents`, `~/Desktop`, and `~/Downloads` project directories in README

### Added
- Add optional Telegram streaming tool call display, controlled by `ENABLE_STREAMING_TOOL_CALLS` and disabled by default

### Fixed
- Preserve streamed tool call prefixes across draft updates, overflow splits, and finalization so displayed tool activity is not lost mid-response
- Align connection resilience test expectations with the current polling connection pool configuration

## [0.9.2] - 2026-03-27

### Changed
- Map existing codebase documentation structure
- General updates and maintenance

## [0.9.1] - 2026-03-22

### Fixed
- Persist structured runtime health to `.telegram_bot/health.json` so `start.sh --status` reports live `starting`, `available`, `degraded`, and `unavailable` states instead of relying on stale logs
- Probe Claude CLI authentication during startup and request handling so status output can distinguish Telegram transport issues from Claude availability issues
- Stop the daemon supervisor before the bot process during `start.sh --stop`, and keep shared token lock files intact when the current bot is not the owner

### Changed
- Expanded health and status regression coverage for runtime cleanup, stale health detection, supervisor shutdown, and component-specific degraded reporting

## [0.9.0] - 2026-03-20

### Added
- Automatically start a new Claude chat when the gap since the previous user message exceeds `AUTO_NEW_SESSION_AFTER_HOURS`, with configuration support and regression tests for session/voice flows

### Fixed
- Make `start.sh --status` report layered bot health so a live process stays `running` even when Telegram networking or Claude SDK calls are degraded
- Treat Claude SDK 403/upstream errors as retryable degraded state in status output instead of incorrectly reporting the bot as unavailable
- Stop the launchd service during `start.sh --stop` so launchd `KeepAlive` no longer immediately respawns the bot and blocks a following `--install`

## [0.8.6] - 2026-03-20

### Fixed
- Use dedicated HTTPX request settings for Telegram polling with HTTP/1.1 and proxy propagation, improving reliability after network changes and proxyed deployments
- Drop stale pending updates on polling restart so the bot resumes with fresh messages after recovery
- Preserve `PATH` and `HOME` in the generated macOS launchd plist so startup service launches reliably outside interactive shells

### Changed
- Updated README documentation for launchd startup behavior and proxy-aware connection recovery

## [0.8.5] - 2026-03-13

### Fixed
- Replace blocking `run_polling()` with low-level async API (`Application.initialize/start/updater.start_polling`) to resolve polling hang where `run_polling()` blocks indefinitely and cannot be interrupted
- Add `getMe()`-based watchdog that probes Telegram API reachability every 60 seconds; after 5 minutes of consecutive failures, stops the updater and restarts polling in-process
- Detect unexpected polling termination and automatically restart without process restart
- Graceful shutdown between restart cycles ensures clean Application teardown

## [0.8.4] - 2026-03-12

### Fixed
- Auto-restart Telegram polling after unexpected exit (e.g. SDK crash triggering graceful shutdown) instead of silently stopping message reception
- Retry transient SDK errors (SIGTERM, SIGKILL, ConnectionRefused) once with automatic reconnection in `process_message`
- NetworkError now retries indefinitely with application rebuild instead of giving up after fixed attempts
- Rapid crash protection: exits only after 5 consecutive polling failures within 30 seconds each

## [0.8.3] - 2026-03-12

### Fixed
- Add network retry logic for connection resilience

## [0.8.2] - 2026-03-10

### Fixed
- Add event loop watchdog that detects zombie state (asyncio loop closed but process alive) and force-exits, allowing start.sh auto-restart to recover
- Enable launchd `KeepAlive` so the service auto-restarts even if start.sh itself exits (e.g. rapid crash limit)

### Changed
- Enhanced `--status` command to detect inactive bots via log mtime checking, reporting detailed diagnostics instead of a misleading "running" status

## [0.8.1] - 2026-03-08

### Fixed
- Volcengine voice transcription now deletes the temporary TOS object after ASR completes, preventing staged voice files from accumulating over time
- TOS cleanup failures are isolated to logs and no longer affect user-facing transcription replies

### Changed
- Extended TOS uploader API to return uploaded object metadata (`object_key` + signed URL) for explicit post-transcription cleanup
- Added tests covering TOS object deletion on both success and failure paths

## [0.8.0] - 2026-03-08

### Added
- macOS voice reply mode with TTS support: bot automatically replies with voice when user sends voice messages, using macOS `say` command + ffmpeg conversion
- Smart voice delivery strategy based on response length (voice-only, text+voice, or text-only fallback)
- `VOICE_REPLY_PERSONA` config for selecting macOS TTS voice persona

### Fixed
- Voice reply mode gracefully falls back to text on non-macOS platforms

### Changed
- Updated README documentation (EN/ZH) with voice reply mode usage guide

## [0.7.0] - 2026-03-08

### Added
- Volcengine ASR support for voice transcription as an alternative to OpenAI Whisper
- TOS (Tencent Object Storage) upload flow for Volcengine ASR integration

### Changed
- Added Star History chart to README files

## [0.6.3] - 2026-03-06

### Changed
- Renamed project from "Telegram Skill Bot" to "Claude Telegram Bot Bridge"
- Updated project name in README.md, README-zh.md, and start.sh
- Changed version display from "Bot version" to "Bridge version"
- Simplified update notification to non-interactive text prompt

## [0.6.2] - 2026-03-06

### Added
- Auto-update check on startup with 1-hour cache to detect new releases
- Interactive upgrade prompt when update is available (upgrade now / skip)
- `--upgrade` command for one-click bot updates via git pull and dependency reinstall
- Version comparison logic to determine if update is needed
- Graceful handling of network failures during update check

### Changed
- Updated README.md and README-zh.md with upgrade command documentation
- Added auto-update feature to Operations section in documentation

## [0.6.1] - 2026-03-05

### Changed
- Simplified bot command descriptions for better user experience in Telegram command menu

## [0.6.0] - 2026-03-05

### Added
- `/revert` command to restore conversation to any previous message state
- 5 revert modes: full restore (code + conversation), conversation only, code only, summarize from point, or cancel
- Paginated history browser showing last 50 messages with inline keyboard navigation
- Priority handling for `/revert`: bypasses message queue limit and cancels active operations
- Interactive mode selection via Telegram inline buttons
- Conversation state restoration by truncating SDK JSONL files to selected message

### Changed
- Updated documentation (README.md, README-zh.md) with `/revert` usage examples
- Improved button text consistency: changed "Never mind" to "Cancel"

## [0.5.0] - 2026-03-05

### Added
- Native Telegram voice message support with automatic transcription via OpenAI Whisper API
- Audio format detection and conversion (OGG/AMR → MP3) using ffmpeg
- Voice message preview in chat: `🎤 Voice: [transcribed text]` before forwarding to Claude
- Priority `/stop` command: immediately cancels running tasks and voice transcription, even when message queue is full
- Comprehensive test coverage for audio processing, transcription, and voice message flow
- Voice configuration options: `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `WHISPER_MODEL`, `MAX_VOICE_DURATION`, `FFMPEG_PATH`
- Automatic cleanup of temporary audio files and stale audio detection
- Retry logic with exponential backoff for Whisper API calls
- Voice message duration validation and cost/duration logging

### Changed
- `/setup` skill now includes optional voice message configuration step
- `.env.example` updated with voice-related configuration options
- Enhanced error handling for voice message processing with user-friendly error messages
- Updated documentation (README, CLAUDE.md) with voice message feature details

## [0.4.0] - 2026-03-04

### Added
- `/setup` skill for conversational, multi-language installation via Claude Code
- Support for installation in any language (English, Chinese, Japanese, Spanish, French, German, etc.)
- Interactive installation wizard with 4-step process (system check, configuration, Python environment, completion)

### Changed
- Renamed `install.sh` to `setup.sh` for consistency with skill naming
- Moved Python virtual environment creation and dependency installation from `start.sh` to `setup.sh`
- `start.sh` now checks for completed installation and provides friendly error message if not installed
- Installation flow now requires running `setup.sh` or `/setup` skill before `start.sh`
- Improved installation prompts with better formatting and clearer instructions
- Fixed color code rendering issues in installation scripts (added `-e` flag to all `echo` commands with color variables)

### Fixed
- Script references in README updated from `install.sh` to `setup.sh`
- Command examples in documentation now reflect new installation flow

## [0.3.0] - 2026-03-03

### Added
- Progressive streaming for AI responses using Telegram draft messages with real-time updates
- Telegram draft API compatibility layer with graceful fallback to regular messages
- Automatic detection of numbered options in responses (not just `AskUserQuestion` tool)
- Streaming configuration via `DRAFT_UPDATE_MIN_CHARS` and `DRAFT_UPDATE_INTERVAL` environment variables

### Fixed
- Duplicate message issue when responses contain option buttons: streamed messages are no longer re-sent
- Improved `AskUserQuestion` denial message with clearer formatting instructions for the AI

### Changed
- Streaming message handler now uses regular `send_message` for initial draft creation to ensure message_id availability
- Large text chunks are split into progressive updates for smoother streaming experience

## [0.2.1] - 2026-03-02

### Added
- Session progress summary: show last assistant message when switching sessions via `/resume`

### Changed
- Remove hardcoded zh-CN language policy; bot preset strings stay minimal English, LLM handles language adaptation naturally

## [0.2.0] - 2026-03-02

### Added
- Long message auto-splitting: responses are split at paragraph/line boundaries (4000-char limit) and sent as multiple messages instead of being truncated
- Typing keepalive loop: background task sends typing indicator at regular intervals during long tool calls to prevent Telegram from dropping the typing status

### Fixed
- Removed 4000-character hard truncation from `_clean_response`; full response content is now preserved
- Inline option keyboard now only appears for `AskUserQuestion` degraded responses (via `force_options` flag), preventing false positives on numbered lists in regular replies

## [0.1.0] - 2026-03-02

### Added
- Telegram bot integration with Claude Code SDK for running Claude sessions from Telegram
- Per-user persistent Claude SDK streams with session history browsing
- Permission gating for file access: auto-allow inside `PROJECT_ROOT`, inline button confirmation for outside
- Message queue per user (max 3 concurrent tasks with overflow rejection)
- `AskUserQuestion` tool degraded to Telegram inline keyboard buttons
- Auto-send media files (photos/documents) when response contains matching file paths
- Session persistence via JSON store (`PROJECT_ROOT/.telegram_bot/sessions.json`)
- Bilingual documentation (English and Chinese)
- `start.sh` lifecycle manager with venv creation, dependency caching, log rotation (14 days), and crash detection
- macOS launchd auto-start support via `--install` / `--uninstall`
- Debug mode with verbose logging and per-session chat file logging
- Proxy support via `PROXY_URL` environment variable
