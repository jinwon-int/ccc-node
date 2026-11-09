- **nunchi: `mempalace-refresh` skips a sweep whose transcripts did not change (#2722 B-2).**
  MemPalace's sweep re-upserts — and so re-embeds — every session's last
  drawer on every run (its `< cursor` tie-break), so an idle hour still
  rewrites about one drawer per session. On a Termux node that cost 22.5
  minutes per hour with `+0 new`, and exact searches during the window failed
  with "Palace changed repeatedly" (9/13 single attempts, 2026-11-09). The
  refresh now fingerprints the sorted `(path, size, mtime_ns)` of every
  `*.jsonl` the sweep would read, saves it (0600,
  `$NUNCHI_HOME/mempalace-refresh.fingerprint.json`) only after a successful
  sweep, and skips the next sweep when the tree is unchanged and the last
  status is `ok`. The skip writes `state=ok` with the previous `index_status`
  and `note=sweep-skipped-unchanged` (the probe ignores unknown keys). A full
  sweep still runs at least every `CCC_NUNCHI_MEMPALACE_MAX_SKIP_SEC`
  (default 6 h). It is on by default only on Termux (detected from `$PREFIX` or
  a Termux `$HOME`, since cron may not export `PREFIX`);
  `CCC_NUNCHI_MEMPALACE_SKIP_UNCHANGED=1/0` forces it on/off anywhere.
  codex/piri (`mine`) are unchanged. The upstream fix — not re-upserting
  unchanged drawers in MemPalace's `sweeper._flush()` — is tracked on #2722.
