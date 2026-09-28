- **Skills: SKILL.md descriptions are written YAML-safe, and quoted ones are
  read back decoded (#2032).** fleet-skills `validate.py` now parses
  frontmatter with PyYAML (fleet-skills#328), so unquoted descriptions with
  `": "` (invalid YAML) or `" #"` (comment truncation) failed six open
  fleet-skills PRs. A shared stdlib renderer (`bridge/utils/skill_frontmatter.py`,
  installed as `~/.claude/hooks/ccc_skill_frontmatter.py`) keeps a description
  plain when YAML reads it back unchanged and otherwise writes one
  double-quoted line (round-tripped through `yaml.safe_load` where PyYAML
  exists). Writers: autosave staging (`skill-review.sh`), the installer
  (`autoinstall.sh`; a missing renderer blocks the draft as
  `lint description-yaml-unsafe`), incremental `SKILL.md` patches
  (`ownership.py`), and the promoter's frontmatter autorepair and reviser
  republish. The promoter's node-side snapshot refuses an unsafe line as
  `skill_description_yaml_unsafe` (autorepair re-renders it); envelopes from
  older nodes are still accepted. Readers decode quoted values with the
  matching helper so quotes never leak into lengths, dedup, listings, or
  registries: `ccc-fleet-skills-sync.py`, `ccc-skill-listing-policy.py`,
  `ccc-skill-registry.py`, `ccc_codex_skills.py`, the promoter, `ownership.py`,
  and the shell readers (`autoinstall.sh`, `extract.sh`, `load-memory.sh`) via
  `claude/hooks/lib/skill-frontmatter.sh`.
