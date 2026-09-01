---
name: gh-ci-wait
description: Register a durable GitHub CI wait when you promise to continue after CI finishes (#740). Use whenever you pushed or opened/updated a PR and would otherwise say "I'll continue once CI is green/failed" — the promise is real only with a wait_id. Never claim auto-resume without one. Also use when SessionStart injects a `⚠ 미완 약속` block and you must triage it — active `⏳` waits must NOT be re-registered, while dropped promises branch on their skip_reason.
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
- After **every push or PR head update**, re-read `headRefOid` and register a
  new wait for that SHA before promising another continuation. A wait is
  one-shot and exact-head-bound: the old `wait_id` never follows the new head,
  even when the PR number and promised next step are unchanged.
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

## After the terminal wake

- The wait resumes exactly one CI-result turn; it does not chain unrelated or
  subsequent work bundles by itself.
- Handle the exact-head result named in the event. If more already-authorized
  work remains afterward, use `$bridge-yield-continue` to register the next
  bundle before ending the wake turn.

## If registration fails (rc != 0)

Never claim auto-resume anyway. Either:
1. keep a **foreground** watch instead (`gh pr checks <n> --watch`), or
2. say plainly that auto-resume is unavailable and the user should ping you
   when CI ends.

`route-unavailable` means the bridge could not bind this conversation —
do not retry blindly more than once.

## Resuming a dropped promise at SessionStart

The SessionStart hook injects a `⚠ 미완 약속` block after a session timeout,
bridge restart, Telegram reconnect, or multi-bundle work that ended early. It
has two halves, and they need **opposite** actions:

**`⏳ 아직 대기 중`** — registrations that are still live (no `skip_reason`).

- **Do NOT re-register.** The wait is alive; a second registration just creates
  duplicate polling against the same head. This is the counter-default move —
  the instinct on seeing a pending list is to re-arm everything.
- Confirm the condition is genuinely still pending (CI still running).
- If a wait has outlived its plausible window (say >4h), investigate whether the
  external system is stuck rather than registering another one.
- Otherwise leave it alone; it self-resolves on its next poll.

**`⚠ 알림은 갔으나 이어가지 못한`** — the notification fired but the
continuation never ran. Each entry carries a `skip_reason`. Branch on it:

- `session_moved` — the session ended before the condition completed. This says
  nothing about whether the external event succeeded. Open the PR, read the
  **current** CI status, and act on what you find now: if it is green and
  mergeable, proceed manually (subject to the usual approvals); if it is still
  pending, register a fresh wait for the **current** head SHA.
- any other `skip_reason` (route/provider/user mismatch) — do not re-register
  blindly. Inspect the live registry with `... external_wait_cli list` and
  confirm the route metadata is restorable first. Re-registering onto a broken
  route reproduces the same drop.

Before acting on any of these entries, remember they are **inherited claims**
written before the session boundary — see `handoff-premise-freshness-verification`
for the write-time vs merge-time check.

**Verification:** the next SessionStart should not re-surface the same promises.
If one does, check whether its `skip_reason` changed or the registry entry
leaked, and audit `external_wait_cli list` against what SessionStart printed.

## User controls

- The user sees waits with `/waits` and cancels with `/cancelwait <wait_id>`.
- You can inspect with `... external_wait_cli list` and cancel with
  `... external_wait_cli cancel <wait_id>` when a wait you own is obsolete.
