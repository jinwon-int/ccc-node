- **Distill: `state/distill.log` is now size-rotated (#1882).** The log was
  appended on every SessionEnd and SessionStart drain and never rotated (one
  node reached 423 MB / 3.7M lines). `distill.sh` and
  `distill/pending-drain.sh` now call the new `hooks/lib/log-rotate.sh`
  before their first append: over `CCC_DISTILL_LOG_MAX_BYTES` (default
  10 MiB) the live file is renamed into `distill.log.1`, older generations
  shift up to `CCC_DISTILL_LOG_KEEP` (default 2) and anything older is
  removed. Rotated generations are gzip'd by a detached `nice` job
  (`CCC_LOG_ROTATE_GZIP=0` keeps them plain), so an existing oversized log is
  archived compressed on the first run after deploy without stalling the hook.
