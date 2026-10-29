- **`memory-guard` now applies the limits to a running bridge unit (#2545).**
  `daemon-reload` alone left an active unit's cgroup at
  `memory.high`/`memory.max` = `max` until its next restart, so the #1877
  guard only changed `OOMPolicy` on live bridges. After writing the drop-in,
  `service-systemd.sh memory-guard` now runs `systemctl set-property --runtime
  <unit> MemoryHigh=<bytes> MemoryMax=<bytes>` on an active unit (percentages
  resolved against `MemTotal`, no restart, MainPID kept) and prints the
  effective `/sys/fs/cgroup/<ControlGroup>/memory.{high,max}`; a cgroup still
  at `max` is reported as not applied and exits 1. Inactive units get no
  runtime change. A rerun on an already-current drop-in still applies the
  runtime step, repairing units guarded by the old reload-only version. New
  `memory-guard --remove [--dry-run]` rolls back: removes the generated
  drop-in, reloads, and clears runtime limits with `MemoryHigh=infinity
  MemoryMax=infinity`. Test seams: `CCC_CGROUP_ROOT`, `CCC_MEMINFO_FILE`.
  ~3 GB-RAM nodes need explicit `CCC_BRIDGE_MEMORY_HIGH/MAX` before a fleet
  rollout (bridge/README.md).
