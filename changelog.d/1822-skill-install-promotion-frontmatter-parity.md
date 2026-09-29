- **skills: the unattended autosave install gate now enforces the promotion
  frontmatter contract (#1822).** Codex-lane drafts with frontmatter the
  promotion snapshot refuses (extra keys such as a nested `metadata:` block,
  blank/comment lines, a padded `---`, a short trailing body) used to pass
  install and stall at promotion as `skill_frontmatter_invalid` — 8 of 19 codex
  installs in the #1353 canary. The contract now lives once in
  `bridge/utils/skill_frontmatter.py` (`strict_frontmatter_fields` plus a
  `check <file>` CLI); `scripts/ccc-skill-promotion.py` and
  `autoinstall.sh` (`gate_promotable_frontmatter`, unattended `run` path only)
  both call it, and a non-promotable draft is blocked before install with
  `lint frontmatter-not-promotable <code>` in `autosave-block.json` and stays
  pending for repair. Owner-approved `apply` is unchanged. The extract prompt
  now states the exact two-key frontmatter shape.
