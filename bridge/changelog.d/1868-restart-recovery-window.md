- **`start.sh --restart`: Termux readiness window 180s, and a longer recovery
  window after a candidate timeout (#1868 owner decision, option 3).** The
  candidate window (`CCC_BRIDGE_RESTART_READY_TIMEOUT`) is a validated
  operator knob (integer 1..3600, refused before stop) whose default is now
  180s on Termux (90s elsewhere, unchanged). When the candidate failed by
  readiness timeout, the one-shot `--recovery-source` attempt waits
  `max(2 × candidate window, 180s)` (360s on Termux, 180s elsewhere) instead of
  the same window, so a slow device no longer fails the candidate and the
  retained recovery for the same reason; after a start error it keeps the
  candidate window. `CCC_BRIDGE_RESTART_RECOVERY_READY_TIMEOUT` overrides it,
  and an exported `CCC_BRIDGE_RESTART_DEADLINE_EPOCH` clamps it to end 60s
  before the outer watchdog (floor 1s), whose process-group kill would take
  the recovery bridge with it. Launch-attempted failures print a
  `ccc-restart-outcome:` JSON line (timeout vs start-error, recovery result,
  and whether any bridge process is serving, alive, or dead).
