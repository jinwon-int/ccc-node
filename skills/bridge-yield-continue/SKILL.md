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

4. Require `{"ok": true, "continuation_id": "..."}` before claiming automatic continuation. A natural-language promise is not a baton.
5. End the turn normally after registration so the bridge can start the queued autonomous turn. Do not keep the turn occupied with a foreground wait.
6. If registration fails, continue locally when feasible or state that automatic continuation is unavailable. Do not retry a route failure more than once.

Keep the prompt body-free and under 4,000 characters. Include no credentials, message bodies, or CI logs. Registration preserves the user's existing authorization; it never grants approval to merge, deploy, delete, or expand scope.

User controls remain authoritative: `/stop` cancels queued and running batons; `/continue` re-arms a cap-held baton.
