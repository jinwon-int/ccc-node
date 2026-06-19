---
name: wiki-record
description: Record durable knowledge to the Seoyoon Family Wiki via the PR-first flow (wiki-agent write-path -> edit in the worktree -> wiki-agent pr). Use whenever you need to durably record a decision, runbook, node fact, incident, or operating-log entry. Handles the section-ID conventions (TM-/ND-/LOG-), the worktree path, and the no-raw-secrets rule.
---

# wiki-record — durable Wiki recording (PR-first)

Use this when work produces reusable operating knowledge (a decision, runbook, node fact, incident, or log entry) that belongs in the Seoyoon Family Wiki. Never paste raw secrets — record only locations / handling rules (FW-03).

## Procedure

1. **Open a PR worktree**
   ```bash
   wiki-agent write-path        # resets/creates the PR branch
   # worktree: /root/.wiki-agent/wiki-pr-work/seoyoon-family-wiki
   ```

2. **Find the target page** (consult first; don't guess paths)
   ```bash
   wiki-agent find "<topic>"
   ```
   Routing: node facts -> `pages/nodes/<name>/`; agent work memory -> `pages/team/<name>/`; people/accounts -> `pages/owners/`; plus `runbooks/services/incidents/decisions/log`.

3. **Compute new IDs** (worktree = `$W`)
   ```bash
   W=/root/.wiki-agent/wiki-pr-work/seoyoon-family-wiki
   # next section id (DOC-/TM-/ND- share one space): max+1
   grep -rhoE "\[(TM|ND)-[0-9]+\]" "$W/pages" | grep -oE "[0-9]+" | sort -n | tail -1
   # next log id:
   grep -hoE "\[LOG-[0-9]+\]" "$W/pages/log.md" | grep -oE "[0-9]+" | sort -n | tail -1
   ```
   - New section heading: `## [TM-<max+1>] <title>` (or `ND-` for node RUNBOOK pages).
   - Log entries are **prepended at the very top** of `pages/log.md` as `## [LOG-<max+1>] <YYYY-MM-DD KST> — <title>`.

4. **Edit in the worktree** with Read/Edit (Read the file first). Keep it public-safe:
   - No tokens, keys, cookies, private keys, real phone numbers, or endpoint values — only locations + handling/rotation rules.
   - Cross-link related pages. Mark uncertain items "확인 필요". Don't delete stale content — mark "상태: 폐기/대체됨".

5. **Open the PR**
   ```bash
   wiki-agent pr     # creates the PR and enables auto-merge
   ```

## Notes
- `log.md` may be edited by other nodes between your read and write — re-Read the top before prepending if the Edit fails.
- One logical change per PR; include the IDs you added in your report back to the user.
