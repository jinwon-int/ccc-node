- **feat(skills): `lifecycle` on promotion ledger rows and `installed.json` (#2283, part 2 of 3).**
  `ccc-skill-promotion.py` derives the stage centrally in `_append_ledger`
  from each row's existing kind/outcome/status, so no write site changes and
  operational rows stay untouched: node stage row → `staged`; kind-less
  publish row (`pr-opened`/`existing-pr`) → `published`; consumed
  `a2a-verdict` → `verdict:<approve|revise|reject>`; `a2a-intake-promoted` →
  `approved`; `a2a-intake-superseded` → `archived:superseded`. An explicit
  `lifecycle` from the caller wins and the caller's dict is not mutated.
  `ccc-fleet-skills-sync.py` writes `lifecycle: "installed@<commit>"` on every
  `installed.json` skill row (duplicating the top-level `commit` on purpose so
  each row is self-contained). Wiki DOC-3903 TM-3906 vocabulary; additive only.
