# Memory subsystem

ccc-node memory starts from a no-network SessionStart snapshot and refreshes caches in the background. The goal is fast startup with bounded context, not exhaustive recall at message time.

## Sources

- Built-in `MEMORY.md` / `USER.md` templates for stable facts.
- Local hot-memory SQLite FTS/fuzzy index.
- Cached Family Wiki prefetch.
- Cached Honcho working memory.
- Distilled local facts from the Session Distiller pipeline.

## Source isolation

- `CCC_NODE_ISOLATION_PROFILE=external` is the higher-priority external-node placement policy. The bridge validates and exports it to Claude hooks; it forces Family Wiki off and the PreToolUse guard rejects Family/internal paths, URLs, commands, and MCP calls before the ordinary approval escape hatch.
- `CCC_WIKI_MEMORY_ENABLED=0` disables the Family Wiki read and write path: no cache injection, refresh, local indexing, distill candidate generation, or Wiki queue writes. Existing cache files are ignored and removed from the local index on its next update/rebuild. An external isolation profile overrides an attempted `=1`.
- `CCC_MEMORY_USER_LABEL` and `CCC_MEMORY_ASSISTANT_LABEL` set the node-local relationship labels used by memory injection and distill. Defaults preserve the existing Seoyoon fleet behavior.
- `CCC_HONCHO_MEMORY_ENABLED=0` disables the Honcho read and Codex write-back path. A node may therefore run built-in/local memory only, Honcho without Wiki, or the default combined profile. `CCC_HONCHO_CFG` selects the owner-only endpoint/credential config (default `~/.hermes/honcho.json`).
- `CCC_BRIDGE_MEMORY_MODE=audience-scoped` derives a distinct Honcho workspace as `<configured-workspace>--ccc-<opaque-scope>`. Shared routes read and write only the shared workspace. Private routes write only their private workspace and may recall that workspace plus the shared workspace and the original configured workspace as private-only legacy input. The legacy workspace is never queried by a group/channel route or copied into shared storage. When Wiki memory is enabled, Codex may generate human-review candidates, but the bridge partitions them under an opaque audience scope and labels every record with its `private` or `shared` audience. No candidate is sent to Family Wiki automatically. `CCC_HONCHO_MEMORY_ENABLED=0` and `CCC_WIKI_MEMORY_ENABLED=0` still disable their respective paths.

## Codex global snapshot materializer

`scripts/ccc_codex_memory.py materialize` reuses `load-memory.sh SessionStart` and writes only a bounded managed block into the active global Codex instructions file. Codex discovery is resolved under `${CODEX_HOME:-$HOME/.codex}`: the first non-empty `AGENTS.override.md` wins, otherwise `AGENTS.md` is used.

The materializer is local/no-provider and preserves user bytes outside `<!-- ccc-node:codex-memory:begin -->` / `<!-- ccc-node:codex-memory:end -->`. It rejects unsafe owners, writable modes, symlinks, hardlinks, non-regular files, malformed markers, and raced active-file changes. Writes use a private same-directory temporary file, fsync, and atomic replace; unchanged snapshot hashes are no-ops. The body-free `.ccc-codex-memory.json` sidecar is safe for diagnostics.

Configuration:

- `CCC_CODEX_MEMORY_MAX_BYTES` — snapshot body cap (default 8192; hard max 24576).
- `CCC_CODEX_AGENTS_BUDGET_BYTES` — whole active global file budget after preserving user content (default 24576; hard max 32768).
- `CCC_CODEX_LOCK_TIMEOUT_SEC` — local materializer lock deadline (default 3 seconds; hard max 10).
- `CCC_CODEX_LOADER_TIMEOUT_SEC` — `load-memory.sh` deadline (default/hard max 14 seconds).
- `CCC_CODEX_MEMORY_LOADER` — explicit trusted loader path when the installed/repository loader cannot be discovered.

`materialize --json` and `status --json` emit only status, hashes, byte counts, active kind, and durability/metadata state; they never emit memory bodies. `setup.sh` installs the materializer and `scripts/ccc-codex` beside `load-memory.sh` under `${CCC_CLAUDE_DIR:-$HOME/.claude}/hooks`.

The launcher runs `materialize` before the real Codex CLI and finishes with `exec`, preserving argv, cwd, stdio, exit status, and signals. A refresh error may use a structurally valid private last snapshot; if `status` is not ready, launch fails closed with exit 78. Configure the underlying binary with `CCC_CODEX_REAL_CLI_PATH` (default `codex`), while `CCC_CODEX_CLI_PATH` points to the installed `ccc-codex` wrapper.

The Telegram Codex runtime invokes the same materializer before every `thread/start` or `thread/resume`. In `audience-scoped` mode, the bridge resolves the Telegram route to an opaque scope and owns a separate app-server with `CODEX_HOME` and `CODEX_SQLITE_HOME` fixed at `CCC_MEMORY_AUDIENCE_ROOT/<scope>/codex`. The materializer accepts scoped mode only when those paths, the private/shared scope label, and `CCC_CODEX_AUDIENCE_AUTH_MODE=keyring` all match exactly. Codex credentials must be provisioned in the operating-system keyring; ccc-node never copies `auth.json` or injects an access token. Session browsing remains disabled on the pooled runtime until browsing commands carry a route audience. `CCC_CODEX_MEMORY_MATERIALIZER_PATH` and `CCC_CODEX_MEMORY_BOOTSTRAP_TIMEOUT_SEC` control the thread-boundary bootstrap. `ccc-memory-check.sh --json` exposes the body-free result under `.codex`.

There is no Codex user-session A2A launch path in current ccc-node main. The #478 Codex distill backend is an isolated extraction boundary that intentionally ignores user config/rules and is therefore **not** routed through `ccc-codex`. Any future A2A worker that starts a user-facing Codex session must use the same wrapper/materializer contract.

## Operating rules

- Startup injection is fail-open and no-network.
- SessionStart local-hot retrieval is read-only and has an inner deadline controlled by `CCC_MEMORY_SEARCH_TIMEOUT_SEC` (default 3 seconds, capped at 10 below the outer 15-second hook limit). A timeout drops only local-hot results; bounded MEMORY/USER/cache/resume blocks still inject.
- Background refresh uses single-flight locking and should not block the interactive session.
- Diagnostics should report counts, statuses, paths, and cache ages only; do not print memory snippets or secrets in fleet reports.
- On Termux, use `${TMPDIR:-$HOME/tmp}` for scratch and keep state under the user's writable home/state directory.

## Codex distill extraction backend

The Codex write-back path is intentionally staged. The provider-neutral boundary
accepts an already bounded `CodexTranscriptSnapshot`, redacts credential-like text,
serializes deterministic input, and validates a strict versioned result.

`CCC_CODEX_DISTILL_CHECKPOINT_TURNS`,
`CCC_CODEX_DISTILL_CHECKPOINT_BYTES`, and
`CCC_CODEX_DISTILL_CHECKPOINT_AGE_SECONDS` configure opt-in write-back
checkpoint gates; all default to `0` (disabled). When multiple gates are
enabled, the first boundary reached after a completed turn records a durable
journal job. Snapshot and extraction work remain asynchronous.

`CCC_CODEX_DISTILL_MODEL` (default `provider-default`) identifies the isolated
extractor model; another safe model ID is passed explicitly to `codex exec`.
`CCC_CODEX_DISTILL_TIMEOUT_SEC` defaults to 120 seconds and is hard-bounded to
1–600. Each completed provider attempt appends body-free accounting to its
journal record: model, bounded snapshot bytes, duration in milliseconds, and
the conservative maximum-token estimate reserved by the shared #388 usage
meter. This estimate is not actual provider token usage. The existing
`CCC_USAGE_BUDGET_TOKENS_CODEX` and `CCC_USAGE_BUDGET_WARN_PERCENT` settings
provide configurable warn/enforce gates; enforce defers autonomous extraction
before claim/provider execution without blocking interactive turns.

- `bridge/memory/distill_extraction.py` contains the input/output models,
  `DistillBackend` protocol, privacy gates, canonical input serializer, strict JSON
  parser, and body-free diagnostics.
- `schemas/codex-distill-extraction-v1.schema.json` is the checked-in provider output
  schema. Every object rejects additional properties. Honcho facts are capped at 12,
  Wiki candidates at 3, Wiki paths are limited to relative `pages/team/...`,
  `pages/nodes/...`, or `pages/log.md` targets, and resume/evidence fields are bounded.
- `CCC_WIKI_MEMORY_ENABLED=0` must be represented to the parser as Wiki-disabled; any
  non-empty `wiki_candidates` result then fails closed.
- Transcript text remains untrusted data. Credential-like content is redacted before
  canonical input serialization, while credential-like or directive-like durable
  Honcho/Wiki output is rejected.
- `bridge/memory/codex_exec_backend.py` implements the isolated provider adapter. It
  launches `codex exec` with `--ephemeral`, `--ignore-user-config`, `--ignore-rules`,
  `--sandbox read-only`, an empty private cwd, checked-in output schema, canonical
  redacted stdin, a minimal allowlisted environment, bounded timeout/cancellation,
  process-group termination, and an owner-only output file. Provider stdout/stderr and
  output bodies are never exposed through errors.
- `bridge/memory/distill_worker.py` claims only completed snapshots, invokes the
  provider-neutral backend behind a fenced extraction lease, and atomically persists
  one strictly validated result or a body-free retryable/terminal failure. Concurrent
  duplicate workers are idempotent, cancellation remains retryable, and stale leases
  resume at extraction without re-reading the user thread.
- The bridge lifecycle schedules bounded snapshot, extraction, audience-routed
  local/resume, and Wiki-candidate workers from the durable journal. Session reset,
  explicit, opt-in checkpoint, and bounded shutdown triggers are supported. The
  Wiki worker is composed only when the fleet Wiki policy is enabled and writes one
  immutable owner-only record per job. Legacy unscoped jobs remain under
  `${BOT_DATA_DIR}/wiki-candidates/<job-id>.json`; audience-routed jobs are physically
  partitioned under `${BOT_DATA_DIR}/wiki-candidates/<opaque-scope>/<job-id>.json`
  and contain explicit `memory_audience` and opaque `memory_scope` labels. Records
  otherwise contain only the strict candidate fields plus hashed provenance and
  remain `review_status=pending`; raw Telegram identities are never serialized. In
  audience-scoped mode, the worker fails closed on a legacy job with no route instead
  of placing it in the global queue. This
  path never invokes `wiki-agent`, writes a Wiki page, creates a branch/PR, or merges.
  Empty candidate sets complete without a queue record. Honcho routing remains under
  an independent lease: legacy jobs use the owner-only
  `${BOT_DATA_DIR}/honcho-outbox/<job-id>.json`, while audience-routed jobs use
  `${BOT_DATA_DIR}/honcho-outbox/<opaque-scope>/<job-id>.json` and deliver to the
  matching physical Honcho workspace with explicit audience/scope labels. Audience
  mode rejects a legacy job with no route rather than sending it globally. Delivery
  uses a stable `Idempotency-Key`; network/config outages keep the scoped outbox
  record retryable without re-extraction, and success acknowledges it. The Honcho
  payload contains strict facts, opaque route labels, and hashed provenance, never a
  raw thread, Telegram numeric identity, transcript, or credential value.
- A completed audience-local sink write refreshes that scope's derived SQLite
  index with the installed `ccc-memory-index.sh` before the journal marks the
  local stage done. The bounded subprocess receives only local path/policy
  variables, disables Wiki/Honcho and optional embedding commands, suppresses
  output bodies, and retries safely after a partial fact commit. This makes a
  newly distilled durable fact available to the immediately following scoped
  Codex materialization without waiting for the next background refresh.
- `scripts/ccc-memory-check.sh --json` reports the journal aggregate under
  `.writeback_queue` without reading any body into its output. It includes queue
  status, valid/pending/invalid counts, journal and snapshot bytes, oldest age,
  retry-attempt counters, and main/local/Wiki status counts. `active` means healthy
  work remains, `settled` means all valid jobs are terminal-successful,
  `degraded` means a retry/failure or unsafe/malformed record was observed, and
  `missing`/`empty` distinguish an uninitialized queue from an initialized queue
  with no jobs. The read-only diagnostic defaults to
  `${BOT_DATA_DIR:-${PROJECT_ROOT:-$PWD}/.telegram_bot}/distill-journal`; tests or
  operators may select another journal with `CCC_DISTILL_JOURNAL_DIR`.
  Its `.writeback_queue.accounting` aggregate reports accounted attempts,
  turn bytes, duration, conservative maximum-token estimates, and safe model
  counts without emitting transcript, extraction, route, or error bodies.

## Useful commands

- `scripts/ccc-memory-check.sh` — body-free read snapshot and write-back queue health.
- `scripts/ccc-memory-index.sh` — local index rebuild/update.
- `scripts/ccc-memory-query.sh` / `scripts/ccc-memory-search.sh` — query/explain recall behavior.
- `scripts/ccc-memory-eval.sh` — no-network smoke/golden/scenario checks.
