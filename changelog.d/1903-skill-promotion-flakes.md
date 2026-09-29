- **skill-promotion: the two causes of the flaky
  `ccc-skill-promotion.test.sh` are fixed (#1903).**
  (1) The promoter's throwaway git clones (`_publish`, `_promote`, the
  promoted-tree read) could fail to clean up. A `git commit`/`fetch` ends by
  spawning a detached `git maintenance run --auto` (older git: `git gc --auto`)
  that keeps writing under `.git/objects` while `TemporaryDirectory.__exit__`
  is deleting the clone. Cleanup then intermittently raised
  `OSError: [Errno 39] Directory not empty`, and the collect ended as
  `internal_error` after the branch was pushed but before the PR or ledger row
  existed. In the suite this was the R2 p3–p6 + #1629 cascade: the p3
  republish crashed, p4 finished it one collect late, and every later phase
  asserted against the wrong round. `_run` now passes
  `-c maintenance.auto=false -c gc.auto=0` to every git call, so no
  background writer is left behind. A production collect that hit this
  recovered on the next cycle through the existing-branch path.
  (2) `ccc_skill_promotion_intake_state_test.py` called `datetime.now()` once
  for each fixture stamp, so the `approved_at` equality pins failed whenever
  the second ticked between the fixture and its expected value. All stamps now
  derive from a single `FIXTURE_NOW`. New regression tests:
  `ccc_skill_promotion_git_background_test.py` (argv guard plus a real-git
  trace2 check that a commit spawns no maintenance child) and
  `FixtureClockTests`.
