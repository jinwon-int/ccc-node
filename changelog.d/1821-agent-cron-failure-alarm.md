- **agent-cron no longer fails in silence (#1821).** A node's claude login
  broke and prompt tasks failed for six days with nothing reaching anyone.
  Failed runs now get a bounded `failureClass`
  (`auth_failed`/`cli_missing`/`timeout`/`other`, from exit code and stderr
  only) recorded in `failure-alarm.json` next to the store — `tasks.json` is
  unchanged, so a revert cannot brick the scheduler. After 3 consecutive
  failures one owner-only alarm is spooled, **regardless of the task `notify`
  setting**: per task (re-alert only on escalation to auth/CLI failure, at most
  once a day) and node-wide for prompt failures spanning 2+ tasks (once per
  streak; resets silently once none of the alerted tasks can run again), each
  with one cleared notice. Opt out with `failureAlertAfter: 0` or
  `CCC_AGENT_CRON_FAILURE_ALERT_AFTER=0` — remove any `failureAlertAfter` from
  `tasks.json` before reverting, since the pre-#1821 schema rejects it. Tasks already mid-streak alert once
  on their first failure after upgrade. `ccc-doctor` warns when prompt tasks
  have not succeeded for more than 7 days (`CCC_DOCTOR_AGENT_CRON_STALE_DAYS`).
