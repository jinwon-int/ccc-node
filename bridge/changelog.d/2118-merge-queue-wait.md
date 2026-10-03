- **External waits can watch a merge queue (#2118).** A queued PR whose
  speculative group run fails is dropped silently — it stays OPEN with its
  approval and no check or state change — so the check-rollup wait never woke
  on it (ccc-node#2113 sat evicted until the owner noticed). `external_wait_cli
  register --source merge-queue` records a `github_merge_queue` wait that polls
  the PR's `isInMergeQueue`/`state` over GraphQL and ends `merged`, `evicted`,
  or `closed`; a pushed head still supersedes. An eviction looks up the newest
  failed `merge_group` run on the PR's `gh-readonly-queue/…/pr-<n>-` branch and
  carries its id in the wake notice and the resume prompt
  (`[external_event: github_merge_queue terminal=evicted … failed_run=<id>]`).
  A PR never seen in the queue is judged only after a 180 s grace
  (`reason=never-enqueued`). `merged` and `evicted` resume the conversation;
  `closed` notifies only. GraphQL errors retry like other transport errors.
  The status message and `/waits` name the merge queue. The default source and
  its prompt text are unchanged.
