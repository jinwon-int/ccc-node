- **Skill autosave: the cross-branch drafting cap now defaults to 3 (#1647).**
  Owner decision 2026-09-29: `CCC_SKILL_AUTOSAVE_TOTAL_MAX_SESSIONS` (the
  per-run drafting cap summed across claude/codex/piri/danso, #1824) is 3 when
  unset instead of no cap, so staging inflow no longer multiplies with the
  number of enabled drafting branches. An explicit value always wins; an
  explicit `0` keeps the documented opt-out (no cap, the pre-#1647 behavior);
  an empty or malformed value now falls back to the default 3 instead of no
  cap. The sweep summary adds `total_max_source=default|env|default-invalid`
  and `status` prints a `drafting budget:` line.
- **Skill autosave installer: `--total-max-sessions N` (#1647).** Bakes
  `CCC_SKILL_AUTOSAVE_TOTAL_MAX_SESSIONS=N` into the managed cron entry
  (inheritable from the environment; non-negative integers only). Like the
  #1867 lane settings, a flagless re-run carries a baked value forward
  (hand-edited values included), reports it on stderr and materializes it into
  the install record argv; `--reset-lane` drops it.
- **Docs: auto-mode nodes are recommended to switch to `set-mode review`
  (#1647).** Recommendation only — each node's switch needs its own operator
  approval; the harness never flips a node's mode.
