- **ccc-doctor: nunchi audience rows judge the live configuration (#2845).**
  `nunchi claude audience map` and `nunchi audience scope` no longer read
  commented-out crontab lines (`#unified-…# … CCC_NUNCHI_AUDIENCE_SCOPED=1 …`)
  as an active scoped lane. Such a line raised a false DEFECT, and its
  `SCOPED=1` also made the scope check return early, which could hide a real
  mismatch. Bridge settings (`CCC_BRIDGE_MEMORY_MODE`,
  `CCC_BRIDGE_MEMORY_AUDIENCE_ROOT`, `BOT_DATA_DIR`) are now read from the
  running bridge's process environment first. This includes unit
  `Environment=`/`EnvironmentFile=` values. Only those three keys are kept, and
  nothing is printed. The doctor's env and the env files are the fallback, and
  a live `BOT_DATA_DIR` picks which data-dir `.env` applies. When a bridge is
  running but its environment cannot be read, an env-file-only
  `audience-scoped` value is reported as `확인 필요` without the
  `--audience-scoped` re-apply advice. That advice would break a healthy
  node-global lane. The DEFECT row now names its `source=`. Measured
  2026-11-21: false DEFECTs on nosuk and soonwook, where the live mode was
  `off`.
