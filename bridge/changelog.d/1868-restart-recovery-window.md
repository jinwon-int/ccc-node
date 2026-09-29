- **`start.sh --restart`: recovery gets a longer readiness window after a
  candidate timeout (#1868).** The candidate window
  (`CCC_BRIDGE_RESTART_READY_TIMEOUT`, default still 90s) is now a validated
  operator knob (integer 1..3600, refused before stop). When the candidate
  failed by readiness timeout, the one-shot `--recovery-source` attempt waits
  `max(2 × candidate window, 180s)` instead of the same window, so a slow
  device no longer fails the candidate and the retained recovery for the same
  reason; after a start error it keeps the candidate window.
  `CCC_BRIDGE_RESTART_RECOVERY_READY_TIMEOUT` overrides it, and an exported
  `CCC_BRIDGE_RESTART_DEADLINE_EPOCH` shrinks it to fit the outer watchdog
  (never below the pre-#1868 window). Launch-attempted failures print a
  `ccc-restart-outcome:` JSON line (timeout vs start-error, recovery result,
  and whether any bridge process is serving, alive, or dead).
