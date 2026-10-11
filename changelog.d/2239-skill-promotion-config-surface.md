- **skill-promotion: the publisher's knobs have one declared surface
  (#2239, follow-up to #2232).** Every `CCC_SKILL_PROMOTION_*` knob used to
  live only in whichever process environment happened to run the promoter, so
  the nightly cron line, the 15-minute results collector and an interactive
  `status` could resolve different values with nothing showing which.
  (1) `ccc-skill-promotion.py` now reads an optional owner-only config file,
  `~/.config/ccc-node/skill-promotion.env` (`CCC_SKILL_PROMOTION_ENV_FILE`
  overrides the path): `KEY=value` / `export KEY=value` lines, `#` comments,
  simple quotes stripped. Only `CCC_SKILL_PROMOTION_*` keys are applied, with
  setdefault semantics (process env wins); everything else is counted and
  dropped. A file that is not a regular 0600 file we own is ignored, never
  fatal. `status` gains `config_file` (path, present, error, applied/overridden
  key names, ignored count — never a value) and `effective` (max_prs_per_run,
  revise_daily_cap, revise_round_limit, collect_window, outbox_stale_hours,
  dispatch_ci_wait_sec, remote_brokers …) so it shows exactly what a cron run
  resolves. (2) `ccc-skill-autosave.sh` sources the same file (owner-only
  check, `set -a`) into the collect child before the edge env and restores the
  process env's own `CCC_SKILL_PROMOTION_*` values afterwards, logs one
  `promotion-env file=<path> present=…` line without values, and `status`
  shows the file next to the edge env line. (3) New
  `scripts/install-skill-promotion-results-cron.sh` renders the publisher's
  `*/15` `collect --results-only --lock-wait 0` entry as a managed
  `# ccc-node:skill-promotion-results:begin/end` block (dry-run default,
  `--apply`, `--remove`, `--status`, gen stamp, install record), placed
  directly below the autosave block's `CRON_TZ` pin or opening with its own
  `CRON_TZ=Asia/Seoul`. `--apply` refuses unless `skill-promotion.publisher`
  reads true (exit 5), detects the legacy ad-hoc
  `skill-pipeline-repair-20261004/run-results.py` line
  (`legacy_results_cron: present`) and replaces it only with
  `--apply --migrate-legacy` (exit 6 otherwise). Registered with ccc-doctor's
  cron marker registry and the gen-stamp inputs. New tests:
  `ccc_skill_promotion_config_file_test.py`,
  `install-skill-promotion-results-cron.test.sh`, autosave case 18.
