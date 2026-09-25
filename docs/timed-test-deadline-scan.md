# Timed-test deadline scan (#1870)

`scripts/timed_test_deadline_scan.py` reports timed tests (canary, observation
window, trial) whose absolute KST end datetime has passed with no verdict
comment. `scripts/install-timed-test-deadline-scan-cron.sh` runs it daily.

## Install

```
scripts/install-timed-test-deadline-scan-cron.sh --dry-run   # preview
scripts/install-timed-test-deadline-scan-cron.sh --apply     # write the crontab entry
```

Default: `20 9 * * *`, `--mode expired`, `--notify high`. The installer does
**not** create `~/.claude/timed-test-deadline-scan.repos` (one `owner/name` per
line, operator-owned); until it exists the scan exits 3 (not configured).

Exit codes: 0 clean, 1 findings (`--exit-nonzero-on-findings`), 2 unusable repo
list, 3 not configured. The full report is appended to
`~/.claude/state/timed-test-deadline-scan.cron.log`.

## Owner notice

With `--notify high|low` (env `CCC_TIMED_TEST_SCAN_NOTIFY`; scanner default
`off`, installed cron `high`) the scanner also queues a short Korean notice in
the owner-only bridge push spool, `${CCC_PUSH_SPOOL:-~/.claude/state/telegram-spool}`,
the same channel agent-cron, `ccc-self-update.sh` and `ccc-pr-status-poll.sh`
use. The bridge PushNotifier (`CCC_PUSH_ENABLED`) delivers it; the scanner never
touches a bot token.

- `high` counts only high-confidence findings; `low` includes the demoted
  false-positive shapes.
- Content: count, then per finding `repo#number`, deadline (KST), days overdue,
  title (≤60 chars, credential-redacted) and URL. First 10 only, then `외 N건`.
  No issue/comment text is copied.
- Dedup: `~/.claude/state/timed-test-deadline-scan.notify-<mode>.json` keeps
  the last notified `repo#number@deadline` set (0600). A new finding notifies
  at once; an unchanged set is re-sent as a reminder after 3 calendar days; a
  clean scan clears it.
- A notice failure is logged on stderr and never changes the exit code. State
  advances only after the spool file is written, so a failure retries next run.
