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
| `operator_review_gated` | **DENIED**; also needs review evidence | `guard.sh` | force-push *to a protected/ambiguous/multi target*, history rewrite, release/publish/tag-push (changes published/shared state) |

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
- **Force-push relaxation** (operator-approved): a *single explicit* force-push to a
  **non-protected feature branch** (e.g. `git push -f origin feat/x`) proceeds
  autonomously — it only rewrites that branch's own history, not shared/published state.
  It stays **DENIED** (review-gated) when the target is a protected branch
  (`main`/`master`/`develop`/`release*`/`hotfix/*`/`prod`/`production`/`stable`), is
  ambiguous/bare (no explicit dst, `HEAD`, current branch), uses multiple refspecs, or is
  part of a compound/chained command. Fail-closed: when the destination can't be parsed
  unambiguously, it is denied.
- Fail-closed: if a pattern is uncertain, prefer gating. Patterns are covered by
  `guard.test.sh` (allow + deny cases, including the force-push relaxation) to avoid
  blocking normal autonomous work.
- This model mirrors the fleet's formal risk profiles and the `CLAUDE.md`
  "Fresh Approval Required" set.

## Memory injection scanner

`scan-injection.sh` protects SessionStart/PostCompact memory injection by redacting
high-risk content before `load-memory.sh` emits `additionalContext`:

- credential-like strings → `[REDACTED:credential]` / `[REDACTED:jwt]`;
- obvious prompt-injection directives → `[REDACTED:prompt-injection]`;
- invisible/bidi Unicode controls → `[REDACTED:unicode]`.

The scanner is **fail-open for availability**: if it is missing or exits non-zero,
`load-memory.sh` injects the original text so a node can still start. Successful findings
are recorded as metadata-only `MemoryInjectionScan` audit events; raw/redacted body text is
never written to the audit log. This is intentionally separate from `guard.sh`, which remains
fail-closed for operator actions.

## Permissions (`settings.json`) vs hook enforcement — decision (#13 item #3)

`settings.json` keeps a broad `Bash(*)` allow **on purpose**: enforcement is the hook
layer, not a permission allowlist. Audit-log analysis (2026-06-21, ~1k tool calls) shows
this node's Bash usage is overwhelmingly **compound / multi-line** — `cd X && …`,
heredocs, `for`/`if` loops, pipes. Claude Code permission entries (`Bash(cmd:*)`)
prefix-match the *whole* command string, so they cannot describe compound commands: a
per-command allowlist would miss most of them and degrade into constant `ask` prompts,
breaking autonomous A2A / cron / headless runs (**over-block**).

Therefore:

- **`allow`** stays broad (`Bash(*)`, `Read/Write/Edit/MultiEdit`), so autonomous work is
  never prompt-blocked.
- **`guard.sh`** (PreToolUse, regex over the *full* command) is the real Fresh-Approval
  enforcement — it sees compound commands that `settings` prefix patterns cannot, and is
  fail-closed.
- `settings` `deny`/`ask` carry only patterns that are *expressible* as a simple prefix
  (secret-file `Read`, `npm publish`, `gh release create`, `sudo`) as a declarative second
  line — never the primary guard. Patterns whose safe form is contextual (e.g. force-push,
  allowed to non-protected feature branches but denied elsewhere) are left to `guard.sh`
  to avoid contradicting its policy.

**Decision:** the roadmap's "replace `Bash(*)` allow-all with an allowlist" (#13 item #3)
is **superseded for this node** by the allow-all + fail-closed-hook model above. The
acceptance goal — *every Fresh-Approval category is code-blocked or prompts* — is met by
`guard.sh` and verified by `guard.test.sh`; an allowlist would add over-block risk without
adding enforcement.
