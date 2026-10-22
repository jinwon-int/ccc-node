- **Test-only: widen the timing budgets behind the merge-group flake in
  `tests/test_project_chat_codex.py` (#2446).** `_wait_until` now allows 5 s
  (was 1 s) and the two ordered-stall tests pin the terminal-stall bound at
  1.0 s (was 0.05 s). Three merge-group failures in one week (3.11 ×1, 3.12
  ×2) hit only these budgets on loaded full-suite runners; no bridge runtime
  code changes.
