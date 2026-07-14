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

`--json` emits only status, hashes, byte counts, active kind, and durability state; it never emits memory bodies. Launcher, Telegram runtime, setup, and doctor wiring are tracked as the next #419 slice.

## Operating rules

- Startup injection is fail-open and no-network.
- SessionStart local-hot retrieval is read-only and has an inner deadline controlled by `CCC_MEMORY_SEARCH_TIMEOUT_SEC` (default 3 seconds, capped at 10 below the outer 15-second hook limit). A timeout drops only local-hot results; bounded MEMORY/USER/cache/resume blocks still inject.
- Background refresh uses single-flight locking and should not block the interactive session.
- Diagnostics should report counts, statuses, paths, and cache ages only; do not print memory snippets or secrets in fleet reports.
- On Termux, use `${TMPDIR:-$HOME/tmp}` for scratch and keep state under the user's writable home/state directory.

## Codex distill extraction boundary

The Codex write-back path is intentionally staged. The current extraction boundary is
pure and provider-neutral: it accepts an already bounded `CodexTranscriptSnapshot`,
redacts credential-like text, serializes deterministic input, and validates a strict
versioned result before any future provider or sink boundary.

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
- This boundary performs no Codex/Claude/provider call, subprocess or network access,
  journal transition, or local/Honcho/Wiki/resume sink mutation. A later child must
  implement an isolated backend and keep the user-facing Codex thread out of the
  extraction context.

## Useful commands

- `scripts/ccc-memory-check.sh` — cache and source health.
- `scripts/ccc-memory-index.sh` — local index rebuild/update.
- `scripts/ccc-memory-query.sh` / `scripts/ccc-memory-search.sh` — query/explain recall behavior.
- `scripts/ccc-memory-eval.sh` — no-network smoke/golden/scenario checks.
