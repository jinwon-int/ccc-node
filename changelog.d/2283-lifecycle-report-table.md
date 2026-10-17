- **feat(skills): lifecycle table in `ccc-skill-usage-report.py` (#2283, part 3 of 3).**
  `node` reports gain a body-free `lifecycle` section read from
  `pending-skills/index.jsonl` (last row per draft wins) and the fleet-skills
  `installed.json` receipt: per-stage draft counts, whole-day pending dwell
  for undecided drafts, malformed-row count and installed rows. Rows from
  before #2283 are derived from `status`/`prescreen` with the `reindex` rules.
  `aggregate` merges them into `lifecycle.stages`, `archive_reasons`,
  `pending_dwell_days` (count/median/p90/max), a per-node table and
  `nodes_without_index`, and the text output prints a "skill lifecycle"
  section. On nosuk's live queue (94 drafts) it reports pending dwell median
  4.0d / p90 8d / max 13d, 6 archived (3 expired, 3 draft-rejected).
