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
- `CCC_HONCHO_MEMORY_ENABLED=0` disables the Honcho read path. A node may therefore run built-in/local memory only, Honcho without Wiki, or the default combined profile.

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

The Telegram Codex runtime invokes the same materializer before every `thread/start` or `thread/resume`. A running resumed thread keeps its already-loaded instructions; a fresh root or cold resume reads the current global snapshot according to Codex behavior. The runtime does not inject `developerInstructions`. `CCC_CODEX_MEMORY_MATERIALIZER_PATH` and `CCC_CODEX_MEMORY_BOOTSTRAP_TIMEOUT_SEC` control this boundary. `ccc-memory-check.sh --json` exposes the body-free result under `.codex`.

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
- The backend is not connected to the journal worker and performs no
  local/Honcho/Wiki/resume sink mutation. Those orchestration and replay-safe sink
  phases remain under #465.

## Useful commands

- `scripts/ccc-memory-check.sh` — cache and source health.
- `scripts/ccc-memory-index.sh` — local index rebuild/update.
- `scripts/ccc-memory-query.sh` / `scripts/ccc-memory-search.sh` — query/explain recall behavior.
- `scripts/ccc-memory-eval.sh` — no-network smoke/golden/scenario checks.
