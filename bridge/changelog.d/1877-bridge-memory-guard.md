- **systemd bridge units get a memory guard (#1877).** One agent-launched
  child growing to ~27 GB was OOM-killed and, under the default
  `OOMPolicy=stop`, systemd SIGKILLed the whole `ccc-telegram-bridge` unit
  with every in-flight session. `service-systemd.sh install` now also writes
  `<unit>.d/10-ccc-memory-guard.conf` (`MemoryHigh=50%`, `MemoryMax=75%`,
  `OOMPolicy=continue`; override with `CCC_BRIDGE_MEMORY_HIGH` /
  `CCC_BRIDGE_MEMORY_MAX`, skip with `CCC_BRIDGE_MEMORY_GUARD=0`). A new
  `memory-guard [--dry-run]` subcommand adds the same drop-in to an already
  installed unit — including `BRIDGE_SERVICE_NAME=ccc-matrix-bridge` — with
  only a `daemon-reload`. Reconcile leaves it alone, so existing nodes change
  only when an operator runs it; the Matrix unit example carries the same
  lines. Hosts without systemd (Termux) are unaffected.
