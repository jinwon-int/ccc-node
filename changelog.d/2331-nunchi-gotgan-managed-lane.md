- **feat(nunchi): managed gotgan refresh lane, Codex harness-prompt exclusion, per-run index budget (#2331 P3-2).**
  `install-nunchi.sh --apply --verbatim-backend mempalace|gotgan|both` (or
  `CCC_NUNCHI_VERBATIM_BACKEND`) now wires an hourly `:27` managed cron line
  for the new `hooks/nunchi/gotgan-refresh.sh` (mode gate, single-flight
  lock, timeout, owner-only body-free status at
  `$NUNCHI_HOME/gotgan-refresh.status.json`), retires the hand-installed
  `# nunchi:gotgan-shadow` line it replaces, records the choice in the install
  record (so a self-update replay keeps the lane) and in
  `$NUNCHI_HOME/verbatim-backend`, which `nunchi.py` reads when no env is set —
  the SessionStart hook path. `gotgan` drops the MemPalace sweep; the default
  `mempalace` is unchanged. `gotgan.py` now rejects Codex harness preambles
  written as user turns (`# AGENTS.md instructions`, `<codex_internal_context>`,
  `<recommended_plugins>`, `<environment_context>`, …; 55% of the shadow node's
  codex USER rows) and ships `gotgan.py prune` for rows an older indexer
  admitted. One `index` run reads at most `GOTGAN_RUN_BYTES` (default 64 MiB,
  floor 1 MiB) and reports `bytes_read`/`files_pending`; the per-file offset
  resumes the backlog on the next run instead of reading whole rollouts into
  RAM (Termux RSS ceiling 2.0 GB).
