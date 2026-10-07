- **agent-cron prompt tasks now honour `timeoutSec` beyond 25 minutes
  (#2156).** The prompt path passes `CCC_HEADLESS_TIMEOUT=<timeoutSec>` to
  `ccc-headless`, whose own `timeout -k 30` wrapper defaulted to 1500s no
  matter what the task asked for — a `timeoutSec: 10800` runbook run was
  killed at 25 minutes with no verification, retry or record step. An
  explicit `CCC_HEADLESS_TIMEOUT` already in the environment still wins. The
  outer `subprocess.run` wait is `timeoutSec + 60s` so the runner's kill grace
  fires first and the run is reported as the runner's exit 124.
