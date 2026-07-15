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
   # worktree: $HOME/.wiki-agent/wiki-pr-work/seoyoon-family-wiki
   ```

2. **Find the target page** (consult first; don't guess paths)
   ```bash
   wiki-agent find "<topic>"
   ```
   Routing: node facts -> `pages/nodes/<name>/`; agent work memory -> `pages/team/<name>/`; people/accounts -> `pages/owners/`; plus `runbooks/services/incidents/decisions/log`.

3. **Compute new IDs** (worktree = `$W`)
   ```bash
   W=$HOME/.wiki-agent/wiki-pr-work/seoyoon-family-wiki
   # next section id (DOC-/TM-/ND- share one space): max+1
   grep -rhoE "\[(TM|ND)-[0-9]+\]" "$W/pages" | grep -oE "[0-9]+" | sort -n | tail -1
   # next log sequence for this KST date and node (replace <node> first):
   KST_DAY="$(TZ=Asia/Seoul date +%Y%m%d)"
   NODE_SLUG="<node>"
   last_seq="$(grep -oE "LOG-${KST_DAY}-${NODE_SLUG}-[0-9]+" "$W/pages/log.md" \
     | sed -E 's/.*-([0-9]+)$/\1/' | sort -n | tail -1)"
   seq=$(( ${last_seq:-0} + 1 ))
   ```
   - New section heading: `## [TM-<max+1>] <title>` (or `ND-` for node RUNBOOK pages).
   - New log entry: `- [LOG-YYYYMMDD-<node>-<seq>] YYYY-MM-DD KST — <title>`.
   - `<node>` is the lowercase executing-node slug: `seoseo`, `dungae`, `sogyo`, `nosuk`, `bangtong`, `yukson`, `soonwook`, `gwakga`, `jingun`, `gongyung`, `daegyo`, or `gongmyoung`.
   - `<seq>` starts at `1` and is `max(seq)+1` among entries for the same KST date and node.
   - Prepend the entry inside `pages/log.md` immediately after the `[LOG-00]` rule block and before the newest existing log entry. Do not put it above `[LOG-00]`.
   - Never assign a new numeric `LOG-NNNN` ID or renumber an old one. When citing an old numeric entry, include its date and title.

4. **Edit in the worktree** with Read/Edit (Read the file first). Keep it public-safe:
   - No tokens, keys, cookies, private keys, real phone numbers, or endpoint values — only locations + handling/rotation rules.
   - Cross-link related pages. Mark uncertain items "확인 필요". Don't delete stale content — mark "상태: 폐기/대체됨".

5. **Open the PR**
   ```bash
   wiki-agent pr     # creates the PR and enables auto-merge
   ```

## Notes
- `log.md` may be edited by other nodes between your read and write — re-Read the top before prepending if the Edit fails.
- After a rebase conflict, update from the latest default branch, recompute this node's same-day sequence, and re-prepend the entry under `[LOG-00]`; never fall back to a global numeric ID.
- One logical change per PR; include the IDs you added in your report back to the user.
