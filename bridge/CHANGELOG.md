# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
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
