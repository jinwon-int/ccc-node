# CLAUDE.md (fixture: current memory span, #2238)

## Instance / Identity
- This node is fixture / fixture.

## Memory Stack
- Current stack: **built-in MEMORY/USER + nunchi (local working memory) + Family Wiki + session_search**.
- **Honcho is retired fleet-wide** (2026-09-01, TM-2029 phase 3; plumbing removed in #1436) — do not query it or treat a missing `~/.hermes/honcho.json` as a fault.
- **Supermemory is retired/legacy** — do not use unless explicitly approved for a specific incident lookup.
- Cleanup policy: latest instructions win; delete/compress boilerplate, duplicates, stale PR/process states, and mutable facts first.

## Live Memory Usage (every session — DO THIS)
The SessionStart hook (`~/.claude/hooks/load-memory.sh`) auto-injects a snapshot at startup: built-in MEMORY/USER + **cached Family Wiki prefetch** + the **nunchi working-memory snapshot** where nunchi is enabled (caches refresh in the background each session). The snapshot is a **starting point, not a substitute** — actively query both sources during work, and the injected Wiki snippets are *candidates* that must be verified before any operational claim.

**Family Wiki — consult FIRST (before web / GitHub / session_search):**
- Search: `wiki-agent find "<query>"`. Verify: `wiki-agent load --lines A:B <path>`. Quick context: `wiki-agent prefetch "<query>"`.
- Durable update: `wiki-agent write-path` → edit in the PR worktree → `wiki-agent pr` (PR-first; never raw secrets, only locations/handling).

**nunchi — local working/relational memory (per node):**
- Recall: `python3 ~/.claude/hooks/nunchi/nunchi.py recall "<query>" [--target <peer>] [--limit N]`; synthesized answer: `nunchi.py dialectic "<query>"`. Facts are per-node candidates — verify before operational claims.

**Routing:** durable facts / nodes / runbooks / decisions → Family Wiki (the cross-node layer). Relational & evolving working memory → nunchi (per node, fed by distill).

## Fresh Approval Required
Node-specific approvals stay here.

## Interaction
| Lane | Policy |
|---|---|
| Wiki / memory reads | Read Family Wiki, nunchi, local MEMORY/USER, and session history. |
