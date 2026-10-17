- **feat(skills): additive `lifecycle` field in the node-local skill ledgers (#2283, part 1 of 3).**
  One vocabulary (Wiki DOC-3903 TM-3906) is now written next to the existing
  fields — nothing renamed, no gate/cap/order change:
  `skill-review.sh` staging → `drafted` (meta.json + index row), draft-gate
  reject → `archived:draft-rejected` (meta + manifest row); `prescreen.py` →
  `prescreened:<verdict>` in prescreen.json, `archived:prescreen-reject` in the
  archive manifest; `pending_expire.py` → `archived:expired`; `autoinstall.sh`
  → `installed@<sha256[:12]>` (legacy path, meta + install ledger) /
  `installed` (incremental path, no per-iteration sha) / `approved` (owner
  `apply`); `/skillsuggest` manual approve → `approved`, reject →
  `verdict:reject`; `curator.py` archive → `archived:curated` (marker + usage
  record, dropped again on restore). `skill-review.sh reindex` derives a
  `lifecycle` for every index row: the directory suffix stays authoritative
  and a meta.json value is only adopted when it agrees (an owner approve leaves
  meta.json at `drafted`); archive rows map `pending-90d` → `archived:expired`.
- **test(skills): install-path suites no longer write into a live danso skills dir.**
  `autoinstall*.test.sh`, `codex-autoinstall`, `curator`, `gate-sim` and
  `ownership*.test.sh` now unset `CCC_DANSO_STATE_DIR`/`DANSO_SKILLS_DIR`.
  An operator shell on a danso node exports them, and the danso lane (#1659)
  resolved its install root from them, so a fixture without an explicit root
  installed `danso-route-skill` into the node's real
  `<state>/home/.pi/agent/skills` (found and removed on nosuk).
