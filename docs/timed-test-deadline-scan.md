# Timed-test deadline scan (#1870)

`scripts/timed_test_deadline_scan.py` reports timed tests (canary, observation
window, trial) whose absolute KST end datetime has passed with no verdict
comment. `scripts/install-timed-test-deadline-scan-cron.sh` runs it three times a day.
The opt-in `--mode followup` also reports follow-ups promised by a verdict that
are past due with nothing posted after them; `scripts/install-timed-followup-scan-cron.sh`
runs it once a day (see below).
`--mode relative` reports timed tests written only in relative terms ("며칠",
"1~2주", "T+24h") with no absolute datetime;
`scripts/install-timed-relative-scan-cron.sh` runs it once a day next to the
expired scan (#2544, see below).

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
- Notices use their own dedup state, `notify-followup.json`, and point at
  `timed-followup-scan.cron.log`.

### Follow-up installer

Followup mode has its own installer, separate from the deadline installer:

```
scripts/install-timed-followup-scan-cron.sh --dry-run   # preview
scripts/install-timed-followup-scan-cron.sh --apply     # write the crontab entry
scripts/install-timed-followup-scan-cron.sh --apply --remove
```

It renders one managed entry (`# ccc-node:timed-followup-scan`, BEGIN/END
block, gen stamp) that runs
`timed_test_deadline_scan.py --repos-file ~/.claude/timed-test-deadline-scan.repos
--mode followup --include-closed-days N --notify LEVEL --exit-nonzero-on-findings`
and appends to `~/.claude/state/timed-followup-scan.cron.log`.

- Defaults: `35 9 * * *` (09:35 KST, once a day, after the deadline scan's
  09:20 run), `--include-closed-days 45`, `--notify high`. Options:
  `--schedule SPEC`, `--include-closed-days N` (non-negative integer, 0 = open
  issues only), `--notify high|low|off`. Env overrides use the
  `CCC_TIMED_FOLLOWUP_SCAN_*` prefix (`_CMD`, `_REPOS`, `_CLOSED_DAYS`,
  `_NOTIFY`, `_CRON`, `_CRON_LOG`).
- Same repos file as the deadline scan; the installer does not create it.
- Every option is written to its own install record
  (`~/.claude/state/install-timed-followup-scan-cron.json`), so a self-update
  replay keeps the schedule, window and notice level.
- Why a separate installer: followup mode reports only followup findings, so
  re-applying the deadline installer with `--mode followup` would replace its
  `expired` entry; install records are one per installer file, so two entries
  from one installer would overwrite each other's record on replay; and the
  marker is not a prefix of `# ccc-node:timed-test-deadline-scan` (or the
  reverse), because the block parser and ccc-doctor match markers as
  substrings. Installing or removing either lane leaves the other's block
  alone. ccc-doctor reports it as `cron gen timed-followup-scan`.

## Relative mode (#2544)

`--mode relative` reports `relative-duration-only`: a paragraph with a
relative duration (`며칠`, `몇 일`, `수일`, `1~2주`, `몇 주`, `당분간`,
`a few days`, `T+24h`, `24시간 후`, `48h 관측`, …) and no absolute date in the
same paragraph. The rule is broken at write time, so the expired scan can
never catch these.

The 2026-10-27 audit measured 7 false positives in 10 findings. Those shapes
are demoted to low confidence (`weak_reason`), not dropped: they stay in the
log, and the installed `--notify high` leaves them out of the owner notice.
One undemoted paragraph keeps the whole issue high; the demoted ones are listed
under `dropped_candidates`. Checked in this order:

| reason code | rule | measured on |
|---|---|---|
| `external-author` | the issue body or comment was written by a non-fleet login (fleet: `jinon86`, `seoseo-ai`, the dependabot / jinwon-deps-bot / github-actions bots). An item with no author data counts as fleet. | esp32-iphone-agent#77 |
| `quoted-term` | every relative term sits inside quotes (`"…"`, `'…'`, `“…”`, `「…」`), inline code, or a fenced code block: rule text or a citation, not a booking | family-messenger#179 |
| `checkbox-verdict-recorded` | a task-list item (`- [ ]`, `- [x]`), and a strict verdict comment (first-line conclusion or a `판정:`/`Verdict:` label line, as in follow-up mode) was posted after it | family-messenger#167 |
| `operational-wording` | the term is `당분간` (a standing arrangement) | esp32-iphone-agent#74, #60, ccc-node#1864 |
| `planning-estimate` | an effort estimate: a planning noun (`스파이크`, `구현`, `개발`, `작업`, `일정`, `spike`, `estimate`, …) within 15 characters before the term, or the term alone in brackets after a plan item (`**D. 푸시**(1~2주)`) | family-messenger#271 |
| `process-duration` | how long an outside procedure takes: `소요`/`걸림`/`takes` right after the term, or `심사`/`발급`/`배송`/`review` just before it | family-messenger#338, #584 |

The last three judge the wording, so they never apply when the paragraph names
an observation (`관찰`, `관측`, `소크`, `카나리`, `모니터링`, `파일럿`,
`시범 운영`, `soak`, `canary`, `monitor`, `observ…`, `pilot`, `burn-in`), and
every unquoted term in the paragraph must be explained by one of them. The
audit's two real findings stay high: a2a-nexus#2395 (`소크: 실제 T2 태스크
수일분`) and family-messenger#404 (`- [ ] 관찰 기간: 공명에서 며칠 운영`, no
verdict yet).

`gh issue list` now also requests `author` (comments already carry theirs).
Notices use `notify-relative.json` and point at `timed-relative-scan.cron.log`.

### Relative installer

```
scripts/install-timed-relative-scan-cron.sh --dry-run   # preview
scripts/install-timed-relative-scan-cron.sh --apply     # write the crontab entry
scripts/install-timed-relative-scan-cron.sh --apply --remove
```

It renders one managed entry (`# ccc-node:timed-relative-scan`, BEGIN/END
block, gen stamp) that runs
`timed_test_deadline_scan.py --repos-file ~/.claude/timed-test-deadline-scan.repos
--mode relative --notify LEVEL --exit-nonzero-on-findings` and appends to
`~/.claude/state/timed-relative-scan.cron.log`.

- Defaults: `25 9 * * *` (09:25 KST, once a day, between the deadline scan's
  09:20 and the follow-up scan's 09:35 runs), `--notify high`. Options:
  `--schedule SPEC`, `--notify high|low|off`. Env overrides use the
  `CCC_TIMED_RELATIVE_SCAN_*` prefix (`_CMD`, `_REPOS`, `_NOTIFY`, `_CRON`,
  `_CRON_LOG`).
- Same repos file as the deadline scan; the installer does not create it.
- Its own install record (`~/.claude/state/install-timed-relative-scan-cron.json`)
  carries every option for self-update replay.
- Separate for the same reasons as the follow-up installer; no marker of the
  three lanes is a substring of another, so installing or removing one leaves
  the others alone. ccc-doctor reports it as `cron gen timed-relative-scan`.
- `install-timed-test-deadline-scan-cron.sh --mode relative` used to replace
  the expired entry. It now delegates to this installer (passing only
  `--apply`/`--dry-run`, `--remove` and an explicit `--schedule`/`--notify`)
  and leaves the expired entry and its record alone. A deadline entry rendered
  with `--mode relative` before #2544 is restored to expired by re-running
  `install-timed-test-deadline-scan-cron.sh --apply`.
- A hand-placed relative block (a crontab block outside the `# ccc-node:`
  markers) is not touched by this installer. Remove it by hand once the managed
  entry is installed, or the scan runs twice.

## Scheduled checks must leave the verdict on the issue (#2544)

Twice in October a persistent timer fired on time, collected evidence and
notified the owner, yet the issue got no verdict comment for 7 days
(family-messenger#179, wiki-agent#218). The scanner then saw an issue with no
judgement and, for those two, only reported it at low confidence. Contract for
any automated recheck (systemd timer, crontab, agent-cron task) that checks a
timed test:

1. It posts its result to the issue it checks, as a comment — a verdict, or a
   **verdict draft** headed `판정 초안` that names who confirms it. A notice to
   the owner or a local evidence file alone is not a recorded verdict.
2. It checks every judgement criterion written in the booking, not only the
   one it can automate. When it cannot evaluate a criterion (for example a
   host-kernel OOM log on another node), the draft says so and does not call
   the closing condition met. family-messenger#179's check read only CI and
   reported "닫는 조건 충족" while the OOM criterion had failed.
3. If it cannot post (no token, read-only lane), it says so in the owner
   notice, so the owner knows the issue still needs a comment.

The scanner does not yet detect "check ran, no verdict on the issue" as its
own signal; until it does, this contract is what keeps it from recurring.

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
