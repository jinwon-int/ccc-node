---
description: Add a durable LOG entry to the Family Wiki via the PR-first wiki-record flow. Arg = the log summary.
argument-hint: [log summary]
---
Record a Family Wiki operating-log entry using the **wiki-record** skill / flow.

**Summary:** $ARGUMENTS

Steps:
1. `wiki-agent write-path` to open the PR worktree.
2. Prepend a new level-2 heading entry `## [LOG-YYYYMMDD-<node>-<seq>] YYYY-MM-DD KST — <title>` at the top of `pages/log.md`, below the digest block (never right under the H1), directly above the newest existing `## [LOG-...]` heading. The `- [LOG-...]` bullet region under the `[LOG-00]` block is the older form; it stays valid but new entries use the heading form, and both regions share one id space. `<node>` is this executing node's lowercase canonical slug; `<seq>` is `max(seq)+1` for that same KST date and node, starting at 1.
3. Write it in the structured style: **맥락 / 변경 / 검증 / 비고**, with IDs, PR links, and commit SHAs. Cross-link any relevant `DOC-`/`TM-` page.
4. Never create a new `LOG-NNNN` ID or renumber an old entry. When citing an old numeric entry, include its date and title. After a rebase conflict, recompute the same-day node sequence and re-prepend under `[LOG-00]`.
5. **No raw secrets** — locations/handling only (FW-03). Scan additions before the PR.
6. `wiki-agent pr` to open + auto-merge the PR; confirm the merge.

If the entry also belongs in a node/team/runbook page (durable fact, not just a log line), update that page in the same PR.
