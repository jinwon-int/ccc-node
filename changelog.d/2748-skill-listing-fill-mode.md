- **feat(skills): listing policy fill mode — describe more skills within the budget (#2748).**
  `ccc-skill-listing-policy` described only core and recently used skills and
  set every other skill to `name-only` even when most of the listing budget
  was free (measured on one node: 310 skills, 21 described, 24% of the budget
  used), so a name-only skill was rarely picked and never became "recently
  used". A new fill mode, **off by default**, describes more skills while the
  estimated listing stays within `F × budget`: first node-local
  autosave-managed skills with no recorded Skill-tool use, then fleet-managed
  skills (most recently installed/updated first, by fleet marker mtime), then
  everything else by name; it stops at the first candidate that does not fit
  and never runs while a recently used skill is over budget. Enable with
  `--fill-budget-fraction F`, env `CCC_SKILL_LISTING_FILL_FRACTION`, or the
  node-local file `~/.claude/skill-listing-policy.fill` (picked up by the
  existing `apply --quiet` in `setup.sh` and the daily cron entry). The safety
  contract is unchanged: only `"name-only"` is ever written (a filled skill
  just gets no entry), operator entries are never modified, a second `apply`
  is a no-op, `release` removes everything owned. Without a switch — or with
  an invalid env/file value, which is ignored with a warning — the result is
  exactly the previous one. `plan` / `apply` report the fraction, the skills
  described by fill per tier and the estimate against the budget.
