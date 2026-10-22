# Nunchi implicit-conflict sweep (ccc-node#2464 제안 2)

`nunchi.py conflict-sweep` finds **implicit** state conflicts — a newer fact that makes an older
one no longer true without negating it or sharing its words ("based in Seattle for years" →
"Austin address updated, utilities being set up"). The write gates cannot see these: G1/G3/B2
compare token overlap, and the STALE bench measured 0/400 detections at write time.

## Authority boundary

An unattended pass may only **flag**. The sweep sets `review=1` on the newer fact and records the
pair in the `conflict_sweep` ledger. It never closes, supersedes, merges or deletes; the owner
resolves the flag (`review <id> --clear`, `supersede`, `merge`). This is the G3 rule (#1739)
extended to vocabulary-disjoint pairs.

## How it works

| Step | Behaviour |
|---|---|
| Newer side | open facts created in the last `--since-days` (7), `review=0`, not `correction`/`constraint`/`observation`, not tagged `auto-distill:` machine rows (#2186); `--target` limits the peer |
| Older side | same peer scope as G3 (`session:*` peers form one pool), same `kind`, still open, older by `valid_from` (id breaks ties): the `--k` (5) newest, plus the `--k` nearest by cosine when `fact_vecs` holds model2vec vectors |
| Ledger skip | pairs already in `conflict_sweep` are not asked again, so re-runs are idempotent and a flag the owner cleared stays cleared |
| Judge | `--judge llm` (default) asks the synthesis backend (`llm_synthesize`, Haiku first) once per 20 pairs for `{"conflicts":[pair numbers]}`. Unparseable output, an out-of-range number or no backend is **fail-closed**: no flag, no ledger row, exit 1, and the next run asks again. `--judge none` lists candidates without a model call |
| Apply | dry-run by default. `--apply` writes each batch's ledger rows and flags in one transaction, re-checking that both facts are still open and the newer one is not already queued |
| Cap | `--max-pairs` (60) per run, newest facts first |

`nunchi.py review` shows the older partner of a sweep flag: `#12 … [conflict-sweep ⟂ #5]`.

`judge-batch.py` treats a recorded conflict partner as a live sibling while the partner is open.
Without that, the deterministic pass (no ≥0.6-overlap sibling → clear) would remove every sweep
flag the next day. Once the partner closes, the flag clears like any lonely flag.

## Measurements (2026-10-23)

| Set | Result |
|---|---|
| STALE 40-scenario fixture, one peer per scenario: M_old + 2 other scenarios' M_old as distractors + M_new, real Haiku judge | recall **32/40 (80%)**, distractor false positives **4/80 (5%)**, failed batches 0 (240 pairs, 12 calls, ~5 min) |
| Same fixture, write gates only (`stale-bench.py` phase A) | 0/40 |
| Read-only copy of one production node DB (968 open facts), `--since-days 7` | 460 candidate pairs; first 60 judged → 0 conflicts |

Not wired into cron. A nightly run costs one model call per 20 new pairs, and installing it
fleet-wide is a separate owner decision.
