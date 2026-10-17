- **fix(skills): an owner `/skillsuggest` reject is `archived:owner-rejected`, not `verdict:reject` (#2373).**
  #2283 recorded the owner's P3 reject with the P6 fleet intake review's
  `verdict:reject` (publisher ledger `a2a-verdict`), so one stage name meant
  two different events and owner rejects were missing from `archive_reasons`.
  `/skillsuggest` reject now writes `archived:owner-rejected`;
  `skill-review.sh reindex` and `ccc-skill-usage-report.py` (`node` rows and
  `aggregate` over not-yet-updated node reports) read a node-index
  `verdict:reject` as `archived:owner-rejected`. The publisher ledger's
  `verdict:*` is unchanged; no field, directory or gate changes.
