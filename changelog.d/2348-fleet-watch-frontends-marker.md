- **fix(fleet-watch): `~/.claude/frontends` is now the Telegram retirement declaration the bridge watch reads (#2348).**
  `fleet-bridge-watch.sh` recognised only the rollback-backup directory
  `~/.claude/state/telegram-retire-<YYYYMMDD>/` (#2344), while `ccc-doctor`,
  `termux-restart-frontends.sh` and `install-hande-bridge.sh` treat the
  operator-owned frontend list `~/.claude/frontends` (#2342/#2346) as canonical,
  so a node that wrote only the list was paged `DOWN` by the watch while doctor
  called it retired. The probe now reads the list from the probing account's
  home with the same parse (`#` comments; space/tab/comma separators; CRLF
  tolerated as in doctor): present without `telegram` reports
  `RETIRED <node> marker=frontends`; present with `telegram` decides over any
  leftover backup directory, so a declared-but-missing bridge still pages
  `DOWN`. The directory marker stays as a legacy fallback when no list exists.
  A running bridge is still judged on its answer regardless of either marker.
  `CCC_FRONTENDS` / `CCC_FRONTENDS_FILE` are intentionally not read by the
  probe (its environment is the watcher's or an ssh login's, not the bridge's).
  `docs/fleet-watch.md` names the list canonical and the directory legacy.
