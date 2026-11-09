- **nunchi: `dialectic` retries a transient MemPalace search failure (#2722).**
  A `mempalace search` that overlaps the hourly `mempalace-refresh` sweep fails
  with an error that names its own remedy — sqlite_exact "Palace changed
  repeatedly during exact search; retry the query", chroma "Error sending
  backfill request to compactor". `_mp_search()` returned `[]` on the first
  attempt, so `dialectic` silently lost its verbatim layer for the whole sweep
  (a Termux node: 22.5 min every hour, 9/13 single attempts failed, 2026-11-09). Only
  those two shapes are now retried, up to `CCC_NUNCHI_MEMPALACE_SEARCH_RETRIES`
  times (default 2, max 5) with a linear backoff of
  `CCC_NUNCHI_MEMPALACE_RETRY_DELAY_SEC` (default 3 s), never past
  `CCC_NUNCHI_MEMPALACE_SEARCH_BUDGET_SEC` (default 60 s) per call. Any other
  failure keeps the old immediate `[]`. A failure, or a success that needed a
  retry, is recorded body-free (result, reason token, attempts, time — never
  the query or excerpts) in `$NUNCHI_HOME/mempalace-search.status.json`
  (0600, atomic). Nothing is printed to stderr, because `bench.sh` captures
  `dialectic` with `2>&1`. The sweep itself rewriting every session's last
  drawer each run is tracked separately on #2722 (proposal B).
