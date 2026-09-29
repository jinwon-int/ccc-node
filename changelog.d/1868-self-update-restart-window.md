- **Self-update: external-restart failures are classified and say who serves
  (#1868).** A failed external restart keeps `result:"restart-failures"` (or
  `runtime-down`) but the audit record now adds `failure_kind` (`timeout`,
  `start-error`, `command-timeout`, `health-timeout`, `command-failed`, ...)
  and a validated `restart_outcome` object parsed from the bridge's
  `ccc-restart-outcome:` line: candidate/recovery causes, exit codes and
  windows, the previously serving PID, and whether a bridge is still serving
  (`available` / `alive` / `dead`). The owner notification names the cause
  and flags `dead` as a service outage. The restart command also receives
  `CCC_BRIDGE_RESTART_DEADLINE_EPOCH` (the end of its command budget). No
  default changes.
