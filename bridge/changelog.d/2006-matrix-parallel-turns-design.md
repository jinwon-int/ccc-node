- **Design only: per-conversation parallel turns for the Matrix frontend
  (#2006).** `docs/matrix-parallel-turns.md` records why the frontend runs one
  turn at a time across every room (a single `work()` loop and single-turn
  transport state; ordering is already per scope), the shared state that a
  parallel runner must first isolate (cross-room sink fallback, approvals,
  `/stop` under `matrix_lock`), and a staged plan that keeps N=1 by default.
  No code or behaviour change.
