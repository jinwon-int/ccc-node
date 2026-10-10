- **nunchi: `constraints-triage` shrinks the open constraint set reversibly (#2222).**
  #2216 bounded what SessionStart injects. The DB itself still held 1,221 open
  constraints on one node, 1,027 of them from one 2026-08 backfill: the same
  phase rule had been re-extracted by up to ~130 worker sessions.
  `nunchi.py constraints-triage` is a dry-run by default. It runs two steps:
  1. **Fold cross-session near-duplicates.** Two constraints match when they
     share the #2216 40-char key or their normalised tokens reach a Jaccard of
     0.6 (`--threshold`). Normalisation strips punctuation and one trailing
     Korean particle. Clustering is greedy and survivor-first (highest
     `source_rank`, then newest), so an inferred row never absorbs a
     user-stated one. Rules that name issue/PR numbers they don't share never
     fold (#1890 guard). Folded rows close the same way as `merge`
     (`merged-away:#S` / `merged:#D`).
  2. **Retire phase-scoped rules.** A rule is retired when it carries a `§`
     section or an issue/PR anchor and is older than 30 days
     (`--retire-age-days`). Its `valid_to` is set and `because` records
     `phase-scoped: <anchors>`. User-stated (rank 3) rules stay unless the
     owner passes `--include-user-stated`.

  Nothing is deleted, and clearing `valid_to` undoes either step.
  `--fold-only`/`--retire-only`, `--sample N` and `--json` shape the report.

  Dry-run and an apply on a **copy** of the measured DB (live DB untouched):
  1,221 → 759 open (362 folded, 100 retired; 696 with `--include-user-stated`).
  The SessionStart "N건 생략" tail drops from 1,058 to 701, and the row count
  is unchanged.
