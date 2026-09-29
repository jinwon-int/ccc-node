- **agent-cron no longer fails in silence (#1821).** A node's claude login
  broke and prompt tasks failed for six days with nothing reaching anyone. Each
  failed run now records a bounded `failureClass`
  (`auth_failed`/`cli_missing`/`timeout`/`other`, never raw output) and
  successes record `lastSuccessAt`. After 3 consecutive failures — of one task,
  or of prompt tasks node-wide — one owner-only alarm is spooled, repeated only
  on a class change and cleared by a recovery notice. The alarm ignores the
  task's `notify` setting; opt out with `failureAlertAfter: 0` or
  `CCC_AGENT_CRON_FAILURE_ALERT_AFTER=0`. `ccc-doctor` warns when an enabled
  prompt task has not succeeded for more than 7 days
  (`CCC_DOCTOR_AGENT_CRON_STALE_DAYS`).
