- **feat(skills): listing policy keep-used tier — a skill with Skill-tool use keeps its description (#2883).**
  A skill that was really used became `name-only` as soon as its last use
  left the 30-day recent window, and a name-only skill can lose to a similarly
  named one (measured on one node's 374-skill listing: 3 of 18 routing items
  picked another skill on every attempt with the right skill name-only, and
  all 3 passed with its description listed; 22 control items were unchanged).
  A new tier, **off by default**, also describes skills with a recorded
  **Skill-tool** use in the last `N` days or at any time (`all`): after the
  recent tier and before fill, most recent use first (then use count, then
  name), while the estimate stays within the full budget; the first one that
  does not fit ends the tier (`skill-used, over budget`), and neither the tier
  nor fill runs while a recently used skill is over budget. A `Read` or shell
  read of a SKILL.md is a view, not a use, and never qualifies (#2881). Enable
  with `--keep-used-days N|all`, env `CCC_SKILL_LISTING_KEEP_USED_DAYS`, or
  the node-local file `~/.claude/skill-listing-policy.keep-used` (picked up by
  the existing `apply --quiet` in `setup.sh` and the daily cron entry). The
  safety contract is unchanged: only `"name-only"` is ever written, operator
  entries are never modified, a second `apply` is a no-op, `release` ignores
  the switch. Without a switch — or with an invalid env/file value, which is
  ignored with a warning — the decisions, the written `settings.json` and the
  text output are exactly the previous ones. `plan` / `apply` report the
  window, the skills described by the tier and those left over budget.
