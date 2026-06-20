# Risk profiles — ccc-node enforcement model

The harness maps every tool action to one of four risk profiles. The two *gated* profiles
are enforced by `guard.sh` (PreToolUse, fail-closed); the other two are non-blocking and
captured by observability (`audit.sh` / `notify.sh`). This implements **separation of
approval from execution**: gated actions do not run without an explicit operator signal.

| Profile | Behavior | Enforced by | Examples |
|---|---|---|---|
| `autonomous` | Proceeds silently | — (not matched by guard) | read/search, normal `git`/`gh`/`npm`, file edits in repo |
| `operator_notify` | Proceeds; recorded | `audit.sh` (PostToolUse) | any mutating tool call (commit, push to a branch, merge) — auditable after the fact |
| `operator_approval_gated` | **DENIED** until `CCC_ALLOW_GATED=1` | `guard.sh` | service control (broker/Gateway/worker/bridge), DB destructive/migrate/replay, repo visibility, secret read/exfil, secret-file Read/Edit/Write, catastrophic `rm` |
| `operator_review_gated` | **DENIED**; also needs review evidence | `guard.sh` | force-push, history rewrite, release/publish/tag-push (changes published/shared state) |

## Bypass (operator approval)

A gated action runs only after the operator approves *that specific action* and sets the
explicit signal:

```bash
CCC_ALLOW_GATED=1 <command>
```

This is the audited bypass. The denial (label + profile + tool, never the raw command) is
recorded to `~/.claude/state/approval-needed.log`; the bypass is logged to stderr.

## Notes

- `operator_review_gated` differs from `operator_approval_gated` only in that the change
  alters shared/published state (remote history, releases), so it warrants review evidence
  in addition to approval. Both are blocked by default.
- Fail-closed: if a pattern is uncertain, prefer gating. Patterns are tuned (see
  `guard.test.sh`, 59 cases) to avoid blocking normal autonomous work.
- This model mirrors the fleet's formal risk profiles and the `CLAUDE.md`
  "Fresh Approval Required" set.
