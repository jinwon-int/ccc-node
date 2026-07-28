---
name: codex-memory-loader-injection
description: Inject extra memory/snapshot content into codex bridge sessions via the CCC_CODEX_MEMORY_LOADER extension point — wrap the base loader, merge JSON, wire .env, restart, and verify the materialize chain. Use when adding a new memory source to codex session startup without touching ccc-node managed files.
---

# Codex Memory Loader Injection

Add a supplemental memory source to codex bridge session startup by wrapping the existing loader extension point, so ccc-node managed files stay untouched and self-update remains safe.

## When to Use
- A new memory/snapshot source must reach codex sessions at startup (e.g., alongside the default load-memory snapshot in AGENTS.md).
- You must NOT edit ccc-node managed scripts (they are overwritten by self-update).

## Procedure
1. **Discover the extension point**: inspect the codex memory script (e.g. `ccc_codex_memory.py`) for the loader env var (e.g. `CCC_CODEX_MEMORY_LOADER`). Confirm the loader contract before writing anything: executable, correct ownership, stdout must be the hook JSON snapshot, and check what arguments the caller passes.
2. **Write a wrapper loader script** (new file, outside ccc-node managed paths): invoke the default/base loader, capture its hook JSON, merge the additional snapshot block into the JSON (parse and re-emit — never string-concatenate JSON), print merged result to stdout. Make it executable.
3. **Test the wrapper standalone**: run it exactly as the bridge would (same args) and validate the output is well-formed hook JSON containing both base and added blocks.
4. **Wire it up**: back up the bridge `.env` (timestamped suffix, e.g. `.env.bak-<purpose>-<yyyymmdd>`), then set the loader env var to the wrapper path.
5. **Restart the bridge** and confirm the service is active with zero errors in logs. Bridge restarts are approval-gated — get fresh approval first.
6. **Trigger/verify materialize**: run the materialize path and check the result JSON: `status` should be `updated`; if `truncated: true`, the size budget was exceeded — explicitly verify the added block survived (grep the target file — e.g. the codex `AGENTS.md` — for a marker from the added block), and note which base content may have been cut.
7. **Record**: file an integration issue in the canonical repo with implementation summary, the budget/truncation observation, and open review items; record a durable log entry via the wiki-record flow.

## Safety
- Never modify ccc-node managed files — extension points only.
- Bridge/Gateway restarts require fresh approval (gated action).
- Always back up `.env` before editing; document the rollback path (remove the one `.env` line + restart; the target file regenerates from the base loader on next materialize).
- No secrets in the wrapper script or logs — read credential locations only.

## Verification
- Wrapper standalone output is valid hook JSON with both blocks present.
- Bridge service active, zero errors after restart.
- Materialize returns `status=updated`; on `truncated: true`, added-block presence in the final target file is confirmed by grep.
- Rollback path tested or at least explicitly stated in the report.
