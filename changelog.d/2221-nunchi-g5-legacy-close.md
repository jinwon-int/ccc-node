- **nunchi: `g5-legacy-close` clears the 2026-08 G5 legacy backlog deterministically (#2221, Tier C).**
  G5 (#1264) never auto-clears a decision without a reason. One node had 425
  such open decisions. All came from the 2026-08 distill backfill, none had
  `source_refs`, and no transcript survived to recover a reason. Deterministic
  reason extraction (Tier A) found a reason clause in only 30. The owner chose
  a deterministic, LLM-free cleanup instead.
  `nunchi.py g5-legacy-close` is a dry run by default. It only touches open
  decisions that fail the shared G5 predicate, are below rank 3 (user-stated
  rows are never touched) and are legacy backfill rows: distill author,
  `created_at` in 2026-08, empty `source_refs`, `distill:`/`auto:update:`
  evidence.
  1. **Retag progress logs.** A row is retagged to `task-progress` when it ends
     on a completion, hand-off or in-flight marker (`완료`, `인계`, `착수`,
     `진행 중`, …) and contains no decision word (`결정`, `하기로`, `확정`,
     `보류`, …).
  2. **Policy close.** Every other row gets `because` = `legacy backfill
     2026-08: original reasoning not recoverable (no transcript); closed by
     #2221 Tier C`.

  The review flag is left unchanged, as with `annotate`, and judge-batch
  re-triages the row once G5 no longer holds it out. No row is closed or
  deleted. `--apply` runs in one transaction, re-checks each row as it writes
  it, and records every change in the append-only `g5_legacy_close` table.
  Running it again is a no-op. `--close-only`/`--retag-only`, `--sample N` and
  `--json` shape the run. `curation-authority.test.sh` fails if the command
  ever writes `valid_to` or `review`.
