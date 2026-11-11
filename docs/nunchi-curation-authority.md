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
| task-progress TTL (opt-in, #2759) | same sweep step as the observation TTL | **off by default.** With `NUNCHI_TASK_PROGRESS_TTL_DAYS` > 0: close `kind=task-progress` facts older than that many days, except user-stated ones (`source_rank` 3), review-flagged ones and facts this sweep already closed once; one body-free `ttl_close` ledger row per close | `nunchi.py` `_close_expired_task_progress` |
| judge-batch | hourly cron (`install-nunchi.sh`, managed marker) | `review=0` only, and only with `NUNCHI_JUDGE_APPLY=1`; supersede appears as proposal text | `judge-batch.py` `apply_clear` |
| conflict-sweep (opt-in) | daily cron when installed with `install-nunchi.sh --conflict-sweep` (#2479) | `review=1` on the newer fact plus a `conflict_sweep` ledger row; never closes | `nunchi.py` `conflict_sweep` |
| schema migration | first open | backfill derived columns (`mutability`) | `nunchi.py` `_migrate_gate_cols` |

A lower-rank fact never closes a higher-rank one (G2), so an agent inference cannot bury a user
statement through G1 or B2.

### Task-progress TTL (#2759)

G1 closes an in-flight fact only when a matching completion fact arrives. A task that was dropped,
renamed or finished without a matching sentence stays open forever: on 2026-11-11, 2,789 of the
5,078 open `task-progress` facts fleet-wide were older than 30 days. The TTL is the age gate for
that class.

- **Off unless set.** `NUNCHI_TASK_PROGRESS_TTL_DAYS` unset, `0`, negative, non-finite or
  unparsable all mean no sweep, and a node that never enables it never gains the ledger table.
  Enabling it (for example `30`) is a per-node owner decision. The sweep runs inside `snapshot`
  and `assemble`, so the variable has to be in that process's environment. For the 10-minute feed
  cron that means a `NUNCHI_TASK_PROGRESS_TTL_DAYS=30` environment line in the crontab above the
  managed lines: `install-nunchi.sh` re-renders the managed lines and does not stamp this
  variable. That one place is enough — a SessionStart `assemble` without the variable simply does
  not sweep. Remove the line to turn it off again; nothing already closed is reopened by that.
- **What it never closes.** A user-stated fact (`source_rank` 3): an owner statement is not closed
  by a lexical or age rule (G2). A review-flagged fact: it is waiting for a verdict. Any other
  kind.
- **Reversible, once.** The close sets `valid_to` only. `ttl_close` (fact id, kind, TTL in force,
  timestamp; no fact text) lists exactly what the sweep closed, so
  `UPDATE peer_facts SET valid_to=NULL WHERE id IN (SELECT fact_id FROM ttl_close)` undoes it. A
  fact is closed by age at most once: after the owner reopens it, later sweeps leave it open.
- **Same failure posture as the observation TTL.** It runs in the same opportunistic transaction:
  when the write lock is busy the sweep is skipped with one stderr line and `snapshot`/`assemble`
  continue read-only.
- **Visibility.** `nunchi.py metrics` prints `task_progress_open`, `task_progress_open_over_30d`
  (reported with the TTL off too, to size the decision), `task_progress_ttl_days` (0 = off) and
  `task_progress_ttl_closed` (ledger count). The weekly bench sheet records them.

## Tier 2 — owner-invoked bulk passes (dry run by default)

| Command | Writes with the apply flag |
|---|---|
| `review-stale [--close]` | close facts the retro G1 pass finds stale |
| `constraints-triage [--apply]` | fold near-duplicate constraints into a survivor (`merged-away:#id`) and close phase-scoped ones (`because` records why) |
| `conflict-sweep [--apply]` | `review=1` on the newer fact plus a `conflict_sweep` ledger row; **never closes** ([nunchi-conflict-sweep.md](nunchi-conflict-sweep.md)) |
| `g5-legacy-close [--apply]` | on the 2026-08 G5 legacy backlog only: retag progress-log rows to `task-progress`, set a fixed policy `because` on the rest, plus a `g5_legacy_close` ledger row; **never closes, never touches `review`** (see below) |

Scheduling one of these turns it into a Tier 1 pass, so it then has to meet the Tier 1 rule: a
sweep that closes facts cannot simply be put on a cron line. `review-stale` and
`constraints-triage` close facts and stay owner-invoked. `conflict-sweep` only flags, so it may be
scheduled: `install-nunchi.sh --apply --conflict-sweep` adds an opt-in daily line (#2479, see the
Tier 1 table), and the enforcement test below keeps that line flag-only.

### `g5-legacy-close` — G5 legacy backlog, Tier C (#2221)

```
nunchi.py g5-legacy-close [--apply] [--close-only|--retag-only] [--sample N] [--json]
```

G5 (#1264) flags a `kind=decision` fact that carries no reason, and nothing clears that flag
automatically. Most of the backlog comes from the 2026-08 distill backfill. Those rows were
stored before G5 existed, carry no `source_refs` and have no surviving transcript, so their
reason cannot be recovered. Extracting a reason deterministically worked for too few rows
(issue #2221). The owner chose a deterministic, LLM-free cleanup instead.

- **Scope.** The command only touches open `kind='decision'` rows that fail
  `_g5_reasonless_decision` (the shared G5 predicate, also used by judge-batch). A row must also
  be a legacy backfill row: `observer=family-assistant`, `created_at` in 2026-08, empty
  `source_refs`, and `evidence` starting with `distill:` or `auto:update:`. User-stated rows
  (`source_rank` 3) are never touched, and there is no opt-in flag for them. Rows with `manual:` or
  `auto-distill:` evidence, and rows created after 2026-08, are reported as `kept_not_legacy`.
- **Retag.** The command retags a row to `task-progress` (mutability recalculated, as `retag`
  does) when the sentence ends on a completion, hand-off or in-flight marker (`완료`, `인계`,
  `착수`, `진행 중`, `머지됨`, `…함`, or the English `done`/`merged`/…). A trailing
  parenthetical is ignored. Any decision word in the row (`결정`, `채택`, `하기로`, `확정`, `보류`,
  `유지`, `승인`, …) vetoes the retag, so that row gets the policy reason instead.
- **Policy close.** Every other in-scope row gets `because` = `legacy backfill 2026-08: original
  reasoning not recoverable (no transcript); closed by #2221 Tier C`. It works like `annotate`:
  G5 stops flagging the row, and `recall`/`dialectic` show the policy reason as its `(근거: …)`.
- **Review flag.** The flag is left unchanged, as `annotate` leaves it, because it may also owe to
  G2/G3. When G5 no longer holds a row out, the next judge-batch run re-triages it. Until then,
  the snapshot counts it under 검토대기 instead of 근거 결측.
- **Safety.** The command is a dry run by default. It prints counts per action and per rank, the
  review-flagged counts and `--sample N` rows (default 20, text cut to 120 chars).
  `--close-only` skips the retag (log rows get the policy reason too) and `--retag-only` skips
  the close. `--apply` runs in one `BEGIN IMMEDIATE` transaction and re-checks every row when it
  writes it (still open, still a decision, still reasonless, still below rank 3). Each change is
  recorded in the append-only `g5_legacy_close` table (fact id, action, old/new kind, rank,
  review flag, timestamp; no fact text). Running it again changes nothing. Like
  `constraints-triage`, it does not take a backup itself: back up `facts.db` before `--apply`.
- **Undo.** To revert a retag, run `nunchi.py retag <id> decision`. To revert a policy close,
  clear `because` in SQL, since `annotate` refuses an empty reason. The ledger lists the ids.

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
- `g5-legacy-close` writes `valid_to` or `review`, or writes `peer_facts` other than `because`
  or `kind`/`mutability`;
- the task-progress TTL defaults to anything but off, loses its `source_rank`/`review`/ledger
  exclusions, or writes anything to `peer_facts` other than `valid_to`;
- the `review` listing or the usage text stops stating the boundary.

When a new pass needs more authority, change this page and the test in the same PR, and state
which tier the pass belongs to.
