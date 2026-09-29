- **Tests: `memory-hooks.test.sh` no longer reaches the network through
  `gh` (#1970).** Its two `refresh-memory.sh` runs left fleet-alert
  collection on, so on a host with a `gh` binary (CI runners ship one,
  unauthenticated) each refresh ran `gh issue list` against two repos with the
  timeout stub stripping the deadline. Both runs now pass
  `CCC_FLEET_ALERTS_COLLECT=0` and a recording, unauthenticated-shaped `gh`
  stub shadows the host binary — the pattern #1973 applied to the freshness
  suite — and two new assertions pin that `gh` is never called and
  `fleet_alerts` reports `skipped`. The suite's refresh stubs now go through
  `write_exec_stub`: their `#!/usr/bin/env bash` shebang could not exec on
  Termux, so the timeout stub silently failed there (and `refresh-memory
  writes source meta without network success` failed) while working on Linux.
