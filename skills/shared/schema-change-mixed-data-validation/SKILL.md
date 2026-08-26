---
name: schema-change-mixed-data-validation
description: Validate data integrity across a schema change (added/removed/retyped fields) by pinning the changeover point from the source commit, segmenting records before and after it, and running a read-only field-presence checklist — so a field missing merely because a row predates the change is never misread as a failure. Use when analyzing logs, audit records, or tables that span a format change.
---

## When to Use

- Recent records carry fields that older records lack (audit logs, JSONL, API responses, config).
- A metric or failure count is being computed over records that span a format change.
- Someone is about to conclude "field absent → the feature failed" without checking record age.
- You must decide whether to backfill/migrate.

## Procedure

### 1. Pin the changeover point from the SOURCE — not from memory

Do not trust a remembered commit/timestamp; verify it:

```sh
git log --oneline -S'<new_field_name>' -- <path>   # which commit introduced it
git show -s --format='%H %ci %s' <commit>          # authoritative timestamp
```

Record: exact commit hash, committer timestamp **with timezone**, and the field's
name, type, scope, and whether it is required or optional. If the deployed
artifact may lag the commit, pin the **deploy** time too — that is the real line,
not the commit time.

### 2. Segment records by the changeover time

- Count records strictly *before* the changeover.
- Count records *after*.
- Identify the mixing zone (one file/table holding both formats) and its boundary row.

State the counting method you used (the exact query/filter), not just the number.

### 3. Name the field-presence risks explicitly

For each added/changed field, write the misreading it invites:

- "`attempts` missing in pre-change rows → could be misread as *fallback failure*
  rather than *schema age*."
- "type changed string → object; new parsing logic will throw on old rows."

Put this as a comment next to any code that reads the mixed data.

### 4. Read-only verification checklist

- New field present in **all** post-change records (sample ≥10%, or all if small).
- Pre-change records **consistently** lack it. Consistency is the signal: all
  missing = expected; *some* present = migration ran mid-batch → investigate.
- Compare both zones side by side. Example:
  ```
  ✓ backend  : present in 15/15 post-change samples
  ✓ attempts : present in 15/15 post-change samples
  ✓ pre-change rows (40): 0 have `attempts` (expected — consistent)
  ```

### 5. Flag anomalies with identifiers, not hypotheses

- No anomalies → "schema change applied cleanly at `<hash>` / `<time>`."
- Some old rows have the new field → migration ran early or was retro-applied; escalate.
- New field present but null → write path may not populate it; escalate.

Report the exact record identifiers (line numbers, row IDs). Do **not** guess a
cause ("probably the schema didn't propagate") and do not skip the value check
("I assume it worked").

### 6. Record the verdict where it will be reused

If a downstream metric or report is built on these records, note the segmentation
rule alongside the number, so the next reader does not recount with a different
method and get a different answer.

## Safety

- **Read-only.** Never modify, backfill, or normalize records during verification.
- **No guessing.** Missing/inconsistent → state facts and escalate; do not hypothesize a cause.
- **Timestamp is the line of truth** — and it must come from the source commit or
  deploy record, not from working memory. A remembered changeover time that is
  wrong invalidates every segment count downstream.
- Backfill/migration is a separate, approval-gated action — never bundled into verification.

## Verification

- Changeover commit hash + timestamp captured from `git show`, quoted in the report.
- Segment counts stated together with the exact counting method.
- Field-presence checklist results recorded and reproducible by re-running the same query.
- Any anomaly reported with concrete record identifiers.
