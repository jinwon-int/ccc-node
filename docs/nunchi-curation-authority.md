# Nunchi curation authority tiers (ccc-node#2464 제안 4)

Nunchi stores per-node working memory in `peer_facts`. Many paths write to that table, and they
differ in who triggers them. This page records **what each path is allowed to do**, so a new pass
can be checked against the tiers before it ships. agent-memory uses the same split: unattended
passes add and update, deletion is a human-confirmed proposal.

## The rule

1. **No path deletes a fact row.** "Removing" a fact means closing it (`valid_to` set). Clearing
   `valid_to` undoes the close, and `supersedes` / `evidence` keep the link to the replacement.
2. **Unattended passes** (ingest hook, cron, snapshot) may only add facts, close facts under a
   deterministic gate, and set or clear the `review` flag. They never rewrite fact text and never
   resolve an ambiguous conflict. Ambiguity goes to the review queue (G3).
3. **Bulk closes beyond the deterministic gates** run only when the owner invokes them. They
   default to a dry run and need an explicit `--apply`/`--close`.
4. **Per-fact edits** (rewrite, merge, reclassify, clear a flag) are owner commands on one id.

`nunchi.py review` lists the queue and prints this boundary on its last line.

## Tier 1 — unattended (no human in the loop)

| Path | Trigger | Allowed writes | Code |
|---|---|---|---|
| ingest | distill pipeline (PreCompact / SessionEnd / auto-distill cron) | insert facts; `review=1` for G2 rank demotion and G3 ambiguous conflicts | `nunchi.py` `ingest` |
| G1 progress→done | inside ingest | close the in-flight fact replaced by a completion fact of equal or higher rank | `nunchi.py` `_close_fact` (G1 branch) |
| B2 correction | inside ingest | close the single best-matching open fact a `kind=correction` replaces (token overlap ≥ 0.5) | `nunchi.py` `_auto_supersede` |
| observation TTL | ingest, `snapshot`, `assemble` | close `kind=observation` facts older than `NUNCHI_OBSERVATION_TTL_DAYS` (7; ≤0 disables) | `nunchi.py` `_close_expired_observations` |
| judge-batch | hourly cron (`install-nunchi.sh`, managed marker) | `review=0` only, and only with `NUNCHI_JUDGE_APPLY=1`; supersede appears as proposal text | `judge-batch.py` `apply_clear` |
| conflict-sweep (opt-in) | daily cron when installed with `install-nunchi.sh --conflict-sweep` (#2479) | `review=1` on the newer fact plus a `conflict_sweep` ledger row; never closes | `nunchi.py` `conflict_sweep` |
| schema migration | first open | backfill derived columns (`mutability`) | `nunchi.py` `_migrate_gate_cols` |

A lower-rank fact never closes a higher-rank one (G2), so an agent inference cannot bury a user
statement through G1 or B2.

## Tier 2 — owner-invoked bulk passes (dry run by default)

| Command | Writes with the apply flag |
|---|---|
| `review-stale [--close]` | close facts the retro G1 pass finds stale |
| `constraints-triage [--apply]` | fold near-duplicate constraints into a survivor (`merged-away:#id`) and close phase-scoped ones (`because` records why) |
| `conflict-sweep [--apply]` | `review=1` on the newer fact plus a `conflict_sweep` ledger row; **never closes** ([nunchi-conflict-sweep.md](nunchi-conflict-sweep.md)) |

Scheduling one of these turns it into a Tier 1 pass, so it then has to meet the Tier 1 rule: a
sweep that closes facts cannot simply be put on a cron line. `review-stale` and
`constraints-triage` close facts and stay owner-invoked. `conflict-sweep` only flags, so it may be
scheduled: `install-nunchi.sh --apply --conflict-sweep` adds an opt-in daily line (#2479, see the
Tier 1 table), and the enforcement test below keeps that line flag-only.

## Tier 3 — owner per-fact commands

| Command | Effect |
|---|---|
| `review <id> --clear` | clear the review flag |
| `supersede <id> <new text>` | close the old fact, insert the replacement with `supersedes` |
| `merge <dup_id> --into <id>` | close the duplicate, append its evidence to the survivor |
| `annotate <id> --because <reason>` | backfill a decision's reason (G5) |
| `retag <id> <kind>` | reclassify a fact |

## Not covered by this rule

`gotgan.py` deletes rows from its chunk and vector index (`chunks`, `chunks_bi`, `vecs`). Those
rows are derived from source files and rebuilt on the next refresh, so they are not memory.

## Enforcement

`claude/hooks/nunchi/curation-authority.test.sh` fails when:

- any nunchi source issues `DELETE FROM peer_facts`;
- `judge-batch.py` writes anything to `peer_facts` other than `review=0`;
- `conflict-sweep` gains a `valid_to` write;
- the `review` listing or the usage text stops stating the boundary.

When a new pass needs more authority, change this page and the test in the same PR, and state
which tier the pass belongs to.
