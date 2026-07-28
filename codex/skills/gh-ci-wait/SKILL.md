---
name: gh-ci-wait
description: Register a durable GitHub CI wait when you promise to continue after CI finishes (#740). Use whenever you pushed or opened/updated a PR and would otherwise say "I'll continue once CI is green/failed" — the promise is real only with a wait_id. Never claim auto-resume without one.
---

# gh-ci-wait — durable GitHub CI wait (#740)

A natural-language "I'll continue once CI finishes" creates **nothing** —
the turn ends and nobody watches GitHub. The promise only becomes real when
you register a durable wait and receive a `wait_id`.

## When to use

- You pushed or opened/updated a PR and the next step depends on the CI
  outcome (merge, address failures, re-review).
- You are about to tell the user "I'll continue when CI finishes".

Do NOT use it for sleeps/heartbeats inside the current turn, and never for
merge/deploy/release actions themselves — CI green never auto-approves
those; the continuation turn must ask for the usual approvals.

## Register (inside the turn)

```bash
python -m telegram_bot.core.external_wait_cli register \
  --repo owner/name --pr <number> --head-sha <exact-head-sha> \
  --summary "<one-line next step>"
```

- Pin the **exact head SHA** you mean (`git rev-parse HEAD`, or
  `gh pr view <n> --json headRefOid`). A newer push supersedes the wait —
  it will never report stale CI as your result.
- `--summary` is the one-line, body-free next step (no secrets, no logs).
  It becomes the continuation prompt: write it so future-you can act on it
  (e.g. "squash-merge PR #123 when green" or "inspect failing checks and fix").
- Optional `--timeout-seconds` (default 6h).

On success the CLI prints `{"ok": true, "wait_id": "..."}`.

## After registering

- Tell the user the `wait_id` and timeout in one short line, then end the
  turn normally. The bridge watches GitHub, notifies the conversation on
  terminal state, and continues with your summary as a bridge-owned
  `external_event` turn.

## If registration fails (rc != 0)

Never claim auto-resume anyway. Either:
1. keep a **foreground** watch instead (`gh pr checks <n> --watch`), or
2. say plainly that auto-resume is unavailable and the user should ping you
   when CI ends.

`route-unavailable` means the bridge could not bind this conversation —
do not retry blindly more than once.

## User controls

- The user sees waits with `/waits` and cancels with `/cancelwait <wait_id>`.
- You can inspect with `... external_wait_cli list` and cancel with
  `... external_wait_cli cancel <wait_id>` when a wait you own is obsolete.
