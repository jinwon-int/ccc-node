- **feat(doctor): `distill empty snapshots` flags zero-byte distill "successes" (#2316 C).**
  A distill job that reaches `snapshot_done`/`extraction_done` on an empty
  snapshot reports success at every layer while the session's memory is lost,
  and has no `error_code`, so `distill snapshot refusals` never saw it. The new
  read-only row counts such jobs from the last 7 days — every
  `extraction_accounting` attempt with `snapshot_bytes=0`, or, without
  accounting, a stored snapshot with `byte_count=0` — per journal label
  (`telegram_bot`, `ccc-matrix`, and the `/danso` journals). It shares the
  regular-file-only journal scan with the refusal check (#2308) and prints only
  counts, timestamps and labels.
