# CLAUDE.md (fixture: seeded before the Honcho retirement, #2238)

## Instance / Identity
- This node is fixture / fixture.

## Memory Stack
- Current stack: **built-in MEMORY/USER + Honcho + Family Wiki + session_search**.
- **Supermemory is retired/legacy** — do not use unless explicitly approved for a specific incident lookup.
- Cleanup policy: latest instructions win; delete/compress boilerplate, duplicates, stale PR/process states, and mutable facts first.

## Live Memory Usage (every session — DO THIS)
The SessionStart hook (`~/.claude/hooks/load-memory.sh`) auto-injects a snapshot at startup: built-in MEMORY/USER + **cached Family Wiki prefetch** + **cached Honcho working memory** (caches refresh in the background each session). The snapshot is a **starting point, not a substitute** — actively query both sources during work, and the injected Wiki snippets are *candidates* that must be verified before any operational claim.

**Family Wiki — consult FIRST (before web / GitHub / session_search):**
- Search: `wiki-agent find "<query>"`. Verify: `wiki-agent load --lines A:B <path>`. Quick context: `wiki-agent prefetch "<query>"`.
- Durable update: `wiki-agent write-path` → edit in the PR worktree → `wiki-agent pr` (PR-first; never raw secrets, only locations/handling).

**Honcho — working/relational memory:**
- Recall (dialectic): `POST {baseUrl}/v3/workspaces/<workspace>/peers/<peerName>/chat` with `{"query":"…","target":"<target>","reasoning_level":"low"}`. `baseUrl` is in `~/.hermes/honcho.json` — never log credential/endpoint values.

**Routing:** durable facts / nodes / runbooks / decisions → Family Wiki. Relational & evolving working memory → Honcho.

## Fresh Approval Required
Node-specific approvals stay here.

## Interaction
| Lane | Policy |
|---|---|
| Wiki / memory reads | Read Family Wiki, Honcho, local MEMORY/USER, and session history; verify Wiki snippets with source loads before operational claims. |
