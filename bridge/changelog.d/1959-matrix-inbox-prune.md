- **Matrix inbox pruning (#1959).** `inbox.sqlite3` no longer grows without
  bound toward the 128 MiB `pilot-storage-limit` gate. At most hourly the sync
  loop replaces `done` job rows finished more than 48 h ago (outside the 24 h
  admission window) with a body-free tombstone (`pruned_jobs`: event id,
  digest, sent event id) and drops their delivery bookkeeping. Replayed sync
  events, re-queued self-job/notice keys, identity conflicts and wait-status
  event lookups behave as with the full row; queued, running, uncertain and
  ready work is never touched. Rows already done at upgrade start their 48 h
  clock at the first open. No VACUUM: freed pages are reused, so the file
  stops growing rather than shrinking.
