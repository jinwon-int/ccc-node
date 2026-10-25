- **Termux prepare: one operator-owned per-node timeout file read by both
  `termux-prepare-generation.sh` and `ccc-self-update.sh` (#2487).** A
  lock-changing update (`reuse=none`) needs a full native build of ~50+ min on
  a slow node, past the 3300 s build default: gongyung stayed on
  `main-fb17fc4-20261021` 2026-10-22..26 after #2433 (certifi) changed
  `bridge/requirements.lock.txt`, each tick ending `native-wheels_timeout`.
  `${XDG_CONFIG_HOME:-~/.config}/ccc-node/termux-prepare.env` (path override
  `CCC_TERMUX_PREPARE_ENV_FILE`; owner-only 0600) holds
  `CCC_TERMUX_PREPARE_TIMEOUT=<1..6900>`; it is parsed as `KEY=VALUE`, never
  sourced (`ccc_termux_prepare_read_env_file`, `scripts/lib/harness-paths.sh`).
  The build uses it as `--timeout-seconds`; self-update raises its prepare-cmd
  budget to value + 300 s (≤ 7200, never lowered). Explicit env wins in each
  script. A symlink, foreign owner, group/world bits or an invalid value make
  the file be ignored with a warning — defaults apply, the update is never
  aborted. The effective value and `source=env|file|default` appear in the
  dry-run/`--status`/build lines, `ccc-self-update.sh status`, the
  `prepare begin` log line and the failure audit (`prepare_budget_source`).
  The wheel-reuse rule is unchanged. `termux-prepare-generation.test.sh` 42→71
  and `ccc-self-update.test.sh` 369→379 PASS (the new cases fail against the
  previous scripts).
