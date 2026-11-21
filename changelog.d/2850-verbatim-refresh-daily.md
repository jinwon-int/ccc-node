- **nunchi: the verbatim-layer refresh runs once a day instead of hourly
  (#2850).** `install-nunchi.sh` now installs the MemPalace sweep at `02:17`
  and the gotgan refresh (`--verbatim-backend gotgan|both`) at `02:47`. Both
  were hourly (`:17`/`:27`). This transcript index feeds only `nunchi.py
  dialectic` and its weekly bench. recall, the SessionStart snapshot and
  `assemble` never read it. Measured on 2026-11-21 over 12 nodes: 4 dialectic
  calls in 28 days against roughly 32,300 s/day of hourly refresh and about
  31.6 GB of index. The owner chose to keep dialectic and lighten its upkeep.
  The verbatim excerpts dialectic quotes can now be up to a day old. Nodes pick
  the new schedule up when self-update replays their recorded install (the
  managed lines carry a new `gen=` stamp). The doctor's sweep age gate
  (`CCC_NUNCHI_SWEEP_STALE_MIN`, default 1560 min = 26 h) already fits a daily
  tick.
