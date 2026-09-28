- **Skill readers decode YAML-quoted SKILL.md descriptions (#2032).** Autosave
  writers now quote descriptions YAML would misread (fleet-skills#328), so the
  family-skills lookup (`core/skill_lookup.py`), explicit skill commands
  (`core/skill_command.py`), and the skill-candidate inventory decode quoted
  frontmatter values with the new shared `utils/skill_frontmatter.py` instead
  of echoing the quotes.
