# PR/issue status poll (ccc-node#962)

## The gap this closes

A session that opens a PR from a bridge identity (e.g. `seoseo-ai`) had no
way to learn later that the PR's state changed — no webhook delivering
GitHub events into the node, and no cron re-checking. `ccc-node#906` sat
**closed as a duplicate 44 minutes after opening**, with CI passing 6
minutes in, but the session kept reporting "CI still running" for two days
because nothing ever told it otherwise and session hand-off carried the
stale claim forward unverified.

This is the **poll** half of the fix (issue #962 proposal 1). It is
intentionally minimal:

- No webhook path (proposal 3): gongyung has no public inbound endpoint
  (Tailscale-only), so a GitHub webhook receiver is real infra work, deferred
  as mid-term.
- No session hand-off pre-flight revalidation hook (proposal 2): a
  complementary hardening layer, worth revisiting once the poll mechanism
  has run for a while — it would touch shared session-start logic used by
  every session, so it stays out of this first, lower-risk cut.

## The procedure

`~/.claude/hooks/ccc-pr-status-poll.sh run` (installed by setup.sh; timer
installed separately via `scripts/install-pr-status-poll-cron.sh`):

1. reads `~/.claude/pr-status-poll.repos` — operator-owned, one
   `<owner/repo> <author>` pair per line, `#` comments, blank lines skipped.
   Missing/empty means **not configured yet**, not "intentionally off" — this
   script does not silently no-op the way an empty `self-update.services` is
   allowed to (Termux genuinely has no systemd to restart); an unconfigured
   poll is a real gap, so leaving it empty is a decision to make visible, not
   a supported steady state.
2. for each pair, lists that author's currently OPEN PRs in that repo and
   derives one overall `checkStatus` (`PENDING` / `SUCCESS` / `FAILURE`) from
   `statusCheckRollup` — a check only counts as done once its `status` is
   `COMPLETED` (or, for legacy commit-status contexts, once `state` isn't
   `PENDING`); once done, only a genuinely bad `conclusion`/`state`
   (`FAILURE`, `CANCELLED`, `TIMED_OUT`, `ACTION_REQUIRED`,
   `STARTUP_FAILURE`, `ERROR`) counts as a failure. A `NEUTRAL` conclusion
   (e.g. CodeQL with nothing to flag) is a normal completed-and-fine outcome
   — see the regression test in `ccc-pr-status-poll.test.sh`, caught via a
   live smoke test against `ccc-node#965`.
3. diffs against the last-seen snapshot (`~/.claude/state/pr-status-poll.json`):
   - a check-rollup transition into `SUCCESS`/`FAILURE` → notify
   - a previously-open PR no longer open → resolve its final state
     (`MERGED`/`CLOSED`) via `gh pr view` → notify
   - first sighting of a repo/author pair seeds the snapshot **silently** —
     there is nothing to diff against yet, so no notification burst on
     rollout or after deleting the state file to force a re-seed
4. notifications go to the push spool only (`~/.claude/state/telegram-spool`,
   `event:"PrStatusPoll"`) — this script never touches the bot token, same
   as `ccc-self-update.sh`.

`ccc-pr-status-poll.sh status` is the read-only inspection mode.

## Installing the timer

```
scripts/install-pr-status-poll-cron.sh --dry-run   # preview
scripts/install-pr-status-poll-cron.sh --apply      # write the crontab entry
```

Default schedule: every 17 minutes. Installing the timer does **not**
configure what gets tracked — an operator still has to populate
`~/.claude/pr-status-poll.repos` (mirrors `~/.claude/self-update.services`).

## Env overrides

`CCC_PR_STATUS_POLL_REPOS`, `CCC_PR_STATUS_POLL_STATE`,
`CCC_PR_STATUS_POLL_GH` (default `gh`; tests inject a fake), `CCC_STATE_DIR`,
`CCC_PUSH_SPOOL`, `CCC_NODE`.
