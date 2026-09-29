- **skills-intake review: a packet-binding mismatch can no longer relax a
  `reject` to `revise` (#1883).** The head_sha / skillName /
  sourceTreeSha256 binding checks ran after the severity floor and assigned
  `verdict = "revise"` unconditionally, so a review with a blocker finding AND a
  binding mismatch — the more suspicious state — was published as
  `revise`/`fail` instead of `reject`/`block`. Every handler-side verdict
  adjustment is now monotonic (only ever stricter); regression tests cover
  blocker + head_sha and blocker + skillName mismatches.
