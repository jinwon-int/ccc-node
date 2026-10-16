- **fix(a2a-review): the skills-intake revise handler names what it saw when no result JSON parses (#2354).**
  The reviser's output used to vanish with the temp dir, and the only trace
  was `HANDLER_FAIL: no parseable revise result JSON in reviser output` plus a
  PR comment saying the task "ended as failed" — three such failures on two
  reviser nodes in two days were undiagnosable after the fact. The failure
  line now carries `bytes=`, `json_objects_found=` and `outcomes_seen=[…]`
  (every `outcome` word found in a JSON object, so a misspelled outcome is
  named), followed by `HANDLER_FAIL_HEAD:`/`HANDLER_FAIL_TAIL:` excerpts of 240
  characters — the same shape #1619 gave the review handler. Exit code and
  no-retry contract (#1460) are unchanged.
