---
description: Add a durable LOG entry to the Family Wiki via the PR-first wiki-record flow. Arg = the log summary.
argument-hint: [log summary]
---
Record a Family Wiki operating-log entry using the **wiki-record** skill / flow.

**Summary:** $ARGUMENTS

Steps:
1. `wiki-agent write-path` to open the PR worktree.
2. Prepend a new `## [LOG-<max+1>]` entry to `pages/log.md` (compute `max+1` from the current top IDs — others may have advanced it). Date it KST.
3. Write it in the structured style: **맥락 / 변경 / 검증 / 비고**, with IDs, PR links, and commit SHAs. Cross-link any relevant `DOC-`/`TM-` page.
4. **No raw secrets** — locations/handling only (FW-03). Scan additions before the PR.
5. `wiki-agent pr` to open + auto-merge the PR; confirm the merge.

If the entry also belongs in a node/team/runbook page (durable fact, not just a log line), update that page in the same PR.
