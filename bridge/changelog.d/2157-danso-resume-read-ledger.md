- **Danso evidence-first resume hands back what the interrupted run already read (#2157).**
  A long task that only read files before it was interrupted (timeout, compaction
  or provider failure) used to restart from zero: the Continue prompt said
  "inspect first", native compaction had already dropped the reads, and nothing
  was on disk — one lane re-read the same eight files 18–22 times across four
  resumes without writing a line. The recovery summary (`danso_recovery._summarize`,
  the same locked journal read as before) now builds a bounded, redacted
  **read ledger** (up to 40 paths with counts, read/write totals, the last three
  agent notes) and `RecoverySnapshot.continuation()` includes it as
  `read_ledger` / `recent_agent_notes` with guidance not to re-read listed files.
  When the run only read (≥60 reads, 0 writes) the prompt asks for a checkpoint
  file (`NOTES-<task>.md`) and the first deliverable before any further reading;
  a file read ≥5 times adds a milder note. The user-facing recovery summary shows
  the same counts. Both Telegram and Matrix use this prompt; safe `/task_resume`
  (native journal resume) is unchanged. Long-task failures
  (`danso_timeout`/`danso_compaction`/`danso_provider`/`danso_provider_timeout`/
  `danso_adapter_error`) now log the last body-free `DANSO_TASK` checkpoint
  (state/stage/requests/tokens/elapsed) for diagnosis. Mid-turn repeat detection
  stays native (`--task-repeat-limit`); the bridge never sees tool bodies.
