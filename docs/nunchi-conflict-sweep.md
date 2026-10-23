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
| Same node, fresh sqlite backup copy (1,210 facts / 968 open), `--since-days 7`, **all 470 pairs judged** (#2479) | 6 conflicts (5 distinct newer facts flagged); hand review: **5 correct, 1 ambiguous, 0 false positives**; 30 random "none" pairs: 0 missed; ~24 Haiku calls, 0 failed batches |

## Scheduling (#2479)

The sweep is opt-in per node. `install-nunchi.sh --apply --conflict-sweep` (or
`CCC_NUNCHI_CONFLICT_SWEEP=1`) adds one managed daily line:

```
53 4 * * * … nunchi.py conflict-sweep --apply --since-days 2 --max-pairs 100 >> ~/.nunchi/conflict-sweep.cron.log 2>&1 # nunchi:#816 gen=…
```

| Choice | Why |
|---|---|
| `--apply` always | the sweep can only flag (see [authority boundary](#authority-boundary)), so there is no dry-run cron mode and no separate APPLY approval like `--judge-apply` |
| 04:53 daily | off every hourly nunchi minute and before the 05:00 fleet-skills sync |
| `--since-days 2` | one day of overlap so a fact landing just after a run is still paired; the ledger keeps the overlap free |
| `--max-pairs 100` | the real-DB run above saw ~67 new pairs/day, i.e. ~4 batched Haiku calls per node per night |

- The flag is materialized in the install record, so a self-update replay keeps the line; an
  explicit re-apply without it removes the line.
- The installer refuses (exit 2, crontab untouched) when the installed `nunchi.py` predates the
  subcommand.
- The judge is `llm_synthesize` (claude → codex → pi, first that answers), not judge-batch's
  provider switch. `NUNCHI_SYNTH_ORDER`, `NUNCHI_PIRI` and `NUNCHI_PIRI_ARGS` set at install time
  are stamped on the line. With no usable backend the run fails closed: exit 1, nothing written,
  the same pairs are asked again next night. A danso-only node needs one of those backends.
- `install-nunchi.sh` status prints `conflict_sweep_cron: <n> line(s)`.
- Rollout: one-node canary for 7 days (review-queue growth, flags per night, false
  positives), then a fleet decision on #2479.
