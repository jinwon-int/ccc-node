- **ccc-doctor: flag an audience-scoped bridge fed by a non-scoped nunchi lane
  (#2075).** A nunchi lane installed without `--audience-scoped` on a bridge
  running `CCC_BRIDGE_MEMORY_MODE=audience-scoped` ingests owner-DM and
  family-room work into the one node-wide store while every tick reads
  healthy. The doctor now adds a `nunchi audience scope` DEFECT row (경고,
  exit code unchanged) when the bridge memory mode — read from the process
  env, then the bot data dir `.env`, then `bridge/.env`, never sourced — is
  `audience-scoped` and the managed nunchi cron lacks
  `CCC_NUNCHI_AUDIENCE_SCOPED=1`. The fix names the re-apply command for
  Claude/Piri lanes (and `--remove` for Codex/Danso, which have no scoped
  lane). `docs/memory.md` documents the re-scope procedure: re-apply,
  quarantine the node-wide `facts.db`/`snapshot.md`/seen list (the carried-over
  `ingested-files` otherwise keeps already-ingested jobs from ever being
  re-routed), let the scoped router reconsider the journal, and verify.
