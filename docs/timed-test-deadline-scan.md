# Timed-test deadline scan (#1870)

`scripts/timed_test_deadline_scan.py` reports timed tests (canary, observation
window, trial) whose absolute KST end datetime has passed with no verdict
comment. `scripts/install-timed-test-deadline-scan-cron.sh` runs it three times a day.
The opt-in `--mode followup` also reports follow-ups promised by a verdict that
are past due with nothing posted after them (see below).

## Install

```
scripts/install-timed-test-deadline-scan-cron.sh --dry-run   # preview
scripts/install-timed-test-deadline-scan-cron.sh --apply     # write the crontab entry
```

Default: `20 9,13,19 * * *` (09:20 / 13:20 / 19:20 KST — daytime only; worst-case
delay for a daytime deadline about 6 h, #2150), `--mode expired`, `--notify high`. The installer does
**not** create `~/.claude/timed-test-deadline-scan.repos` (one `owner/name` per
line, operator-owned); until it exists the scan exits 3 (not configured).

Exit codes: 0 clean, 1 findings (`--exit-nonzero-on-findings`), 2 unusable repo
list, 3 not configured, 4 partial scan. The full report is appended to
`~/.claude/state/timed-test-deadline-scan.cron.log`.

Partial scan (#2324): a repo that cannot be read — Issues disabled, `gh`
failure, timeout, unparsable output — is skipped with a `repo skipped` line on
stderr, listed under `repo_errors` in `--json` output (a `⚠ 부분 스캔` block in
the text report), and every other repo is still scanned and reported. The run
then exits 4 even if findings exist, because the result is known to be
incomplete. Keep repos with Issues disabled out of the repos file.

## Follow-up mode (#2324)

`--mode followup` is opt-in; `expired` and `relative` output is unchanged. It
reports `followup-overdue`: a verdict was posted, and that verdict comment or a
later comment promised a follow-up at an absolute datetime that has passed with
**no comment after it**.

- Keywords: `후속`, `다음 확인`, `재측정`, `재확인`, `재점검`, `재카나리`,
  `next action`/`next check`, `follow-up`. The line must also carry a date.
  Dates are parsed as in the deadline scan: KST by default, and a time marked
  `UTC`/`Z` is converted to KST.
- Verdict: the first comment whose first line states a conclusion (as in the
  early-verdict check) or that has a `판정:`/`Verdict:` label line. The issue
  body and comments before the verdict are not read; those are bookings, which
  `expired` covers.
- Cleared when any comment is posted after the promised time, which counts as
  evidence of action, or when a later comment moves the follow-up to a later
  time.
- Demoted to low confidence (`weak_reason`): `followup-mention` (keyword
  mentioned in passing instead of written as a label such as `다음 확인: …` or
  `| Next action | …`), `verdict-heading-only` (only the looser heading-window
  verdict so far), `cross-reference`, `already-settled`, `claim-eta`,
  `date-only`. A time already past when the comment was posted is a record,
  not a promise (`past-at-posting`), and is never reported.
- `--include-closed-days N` (followup mode only, default 0 = open issues only)
  also fetches issues closed on or after `now - N days` with a second
  `gh issue list --state closed --search "closed:>=YYYY-MM-DD"` query. GitHub
  evaluates that date in UTC. The flag applies to `--repo`/`--repos-file`, not
  `--input`. Using it with another mode, or with a negative N, exits 2.
- In followup mode, `--json` adds `include_closed_days` to the payload and
  `issue_state` (`open`/`closed`, or null when unknown) to each finding. The
  text report marks closed issues `닫힌 이슈`.
- Notices use their own dedup state, `notify-followup.json`. The installer
  still wires only `expired`/`relative`, so for now run followup mode by hand,
  e.g. `--repos-file … --mode followup --include-closed-days 45`.

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
