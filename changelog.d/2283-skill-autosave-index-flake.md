- **test(skills): the #2283 index assertions in `ccc-skill-autosave.test.sh` no longer race the staging child (#2283).**
  `skill-review.sh` stages drafts in a detached setsid child that moves
  `SKILL.md` into place, then writes `meta.json`, appends the
  `pending-skills/index.jsonl` row and only then logs `staged id=…`. The test
  waited for `SKILL.md` alone and counted index rows immediately, so on a
  loaded runner it read 0 rows: the three "direct staging" assertions failed
  together (merge-group run 39034497754; ~1 in 3 local runs on `main`
  `a5c7f54c`). A shared `wait_staged` now waits for `SKILL.md` **and** the
  post-append `staged id=` log line, for both the direct and the sweep path,
  and each wait is an assertion of its own. With a 2 s delay injected between
  the `SKILL.md` move and the index append, the old test fails exactly those
  three assertions (PASS=188 FAIL=3) and the new one passes (193/193). Test
  only; `skill-review.sh` is unchanged.
