---
name: bridge-yield-continue
description: Register a durable bridge baton for the next authorized work bundle and end the current turn cleanly. Use when multi-bundle work remains after the current logical unit, including after handling a GitHub CI external event, and the next unit needs no new user choice or approval.
---

# Bridge Yield and Continue

1. Finish and verify the current logical bundle before handing off.
2. Do not register while waiting on GitHub CI; use `gh-ci-wait` for that exact-head wait.
3. If more already-authorized work remains after the current bundle or CI wake, register exactly one self-contained next bundle:

   ```bash
   python -m telegram_bot.core.continuation_cli register \
     --prompt "<next bundle, starting state, scope, and completion condition>"
   ```

   A concrete, self-contained prompt example (synthetic, illustrative — do
   not run it; a fresh turn with no other context must be able to execute
   it):

   ```text
   Finish the authorized docs sweep in /work/sample-repo, bundle 2 of 2.
   Starting state:
   bundle 1 fixed guides/*.md and appended "guides done" to
   docs/sweep-status.md; reference/*.md is untouched. Scope: fix broken
   anchors and heading levels in reference/*.md, and append the completion
   record to docs/sweep-status.md. No other files are in scope.
   Completion condition: every
   reference/ page renders without anchor warnings,
   docs/sweep-status.md records "bundle 2 done", and the turn ends with
   a one-line summary.
   ```

   This shape matches the CLI's actual interface, verified against
   `bridge/core/continuation_cli.py` (module `telegram_bot.core.continuation_cli`;
   `--help` and source): `register` takes a single `--prompt` value, which
   `validate_prompt` rejects when empty or over 4,000 chars after whitespace
   normalization; exit codes are 0 ok, 2 validation/usage, 3 when no single
   active route exists (fail-closed), 4 queue error; output is one compact
   JSON line. Keep the example's boundary in mind: registration only queues
   work the turn was already authorized to do.

4. Require `{"ok": true, "continuation_id": "..."}` before claiming automatic continuation. A natural-language promise is not a baton.
5. End the turn normally after registration so the bridge can start the queued autonomous turn. Do not keep the turn occupied with a foreground wait.
6. If registration fails, continue locally when feasible or state that automatic continuation is unavailable. Do not retry a route failure more than once.

Keep the prompt body-free and under 4,000 characters. Include no credentials, message bodies, or CI logs. Registration preserves the user's existing authorization; it never grants approval to merge, deploy, delete, or expand scope.

User controls remain authoritative: `/stop` cancels queued and running batons; `/continue` re-arms a cap-held baton.

## Re-verifying the CLI contract

Step 4's success shape is the skill's whole mechanism — if it drifts, the baton
silently stops being a baton. The CLI is a separate component, so check it
rather than trusting this file:

```bash
grep -n '"continuation_id"' bridge/core/continuation_cli.py   # the `_emit` success block
grep -n '"ok": False' bridge/core/continuation_cli.py         # failure shapes, incl. route/queue errors
```

Re-verified 2026-09-10: `continuation_cli.py` emits `{"ok": true,
"continuation_id": ...}` on success and `{"ok": false, "code": ...}` on
failure (`validation`, `queue-error`, and a route failure). If a check
disagrees, the CLI is authoritative — fix this file.
