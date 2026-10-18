- **fix(skills): node-local installs are `local-installed:{auto|owner|manual}`, not `approved` / `installed@<sha>` (#2374).**
  #2283 labelled an autosave install `installed` / `installed@<SKILL.md sha12>`
  and an owner approve `approved`, but Wiki DOC-3903 TM-3906 reserves
  `approved` for the P6 promote merge and `installed@<sha>` for the P7 fleet
  install (`installed.json`, #2369). The real path has a node-local install
  between P3 and P4 — promotion only stages installed autosave-managed skills
  (`ccc-skill-promotion.py` `_snapshot`). Now: `autoinstall.sh` writes
  `local-installed:auto` (meta + install ledger; the content sha stays in
  `.sha256`), `autosave apply` writes `local-installed:owner`, and the
  hand-copied `/skillsuggest` legacy approve (no autosave marker, never
  promoted) writes `local-installed:manual`. `skill-review.sh reindex` derives
  the split from each draft's meta (`installed_by=owner-approved`), and
  `ccc-skill-usage-report.py` reads node-index `installed`/`installed@<sha12>`
  as `local-installed:auto` and `approved` as `local-installed:owner` (a
  pre-#2374 row or node report cannot tell owner from manual). Fleet
  `installed.json` and publisher-ledger stages are unchanged.
