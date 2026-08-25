---
name: metric-counting-method-verification
description: Before quoting a count in a report, decision, or handoff, verify the counting method against the query the system's own code uses — not against a prior session's number. Use when a metric feels inconsistent, when two documents disagree about the "same" number, when inheriting work that cites metrics, or before any gate decision (pilot expand/rollback, threshold alarm, capacity call).
---

# When to Use

- You are about to state a number in a report, issue comment, or handoff.
- Two sources disagree about the "same" metric (prior session vs. live, dashboard vs. alarm).
- A count looks implausible — too round, too large, or unchanged when it should move.
- A gate decision depends on the number (pilot expansion, rollback, threshold alarm).
- You are inheriting work whose conclusions rest on a cited metric.

**The failure this prevents:** quoting a number that is arithmetically correct but
counts a *different population* than the one the decision is about. This does not
look like an error — it looks like a fact.

# Core insight

A metric is a **(query, population)** pair, never just a value. Two correct queries
over the same table produce different, equally "true" numbers. The one that matters
is whichever query the system's own decision logic runs — because that is the number
the alarm fires on, the cron acts on, and the operator sees.

So: **find the code, not the number.**

# Procedure

1. **Write down the claim and its provenance** — the value, and exactly where it came
   from (prior session note, issue body, your own earlier message, a dashboard).
   If provenance is "I computed it earlier," treat it as unverified.

2. **Find the query in source** — grep for the metric's table or the filter fragment
   in the code that *acts* on it (cron job, alarm, status command), not the code that
   merely displays it.

   ```bash
   grep -rn "FROM peer_facts" ~/.claude/hooks/nunchi/*.py | head
   ```

3. **Note every predicate, including the ones that look incidental.** Time cutoffs,
   soft-delete columns, and status flags are where populations diverge.

   Worked example (nunchi review queue, verified 2026-08-25):
   - `nunchi.py:1073` — `SELECT COUNT(*) FROM peer_facts WHERE valid_to IS NULL AND review=1`
   - `judge-batch.py:187` — same, **plus `AND created_at <= ?`**

   Both are correct. The first is the operator-facing queue depth; the second is what
   one batch can actually consume. Quoting the first as "what the batch will clear"
   overstates it. `valid_to IS NULL` is the soft-delete guard — dropping it silently
   folds closed/superseded rows back in and inflates the count.

4. **Run the authoritative query yourself** and compare against the claim.

5. **If they differ, do not overwrite history — annotate it.**
   > Prior count 374 counted closed facts too (no `valid_to IS NULL`); the
   > operationally valid figure is 237 (`valid_to IS NULL AND review=1`).

   Then re-check every conclusion that used the old number. A metric error is rarely
   isolated — it propagates into thresholds, ETAs, and go/no-go calls made from it.

6. **Carry the method with the number.** In reports and handoffs write
   `237 (valid_to IS NULL AND review=1)`, never a bare `237`. A number without its
   predicate cannot be independently re-verified, and the next operator will
   re-derive it differently.

# Safety

- Never let a gate decision (expand, roll back, scale, alarm) rest on an unverified
  count. Verify first, then decide.
- Beware "capacity" vs. "depth" confusions: if the consumer's query has extra
  predicates, queue depth is **not** the drainable amount, and a threshold set against
  depth can latch on permanently.
- Deriving a number from a prior session's note is not verification — that note is the
  thing under test.

# Verification

- [ ] The query used is quoted from source with `file:line`.
- [ ] Every predicate is accounted for, including time cutoffs and soft-delete guards.
- [ ] Any prior divergent number is annotated with both methods, not silently replaced.
- [ ] Downstream conclusions that used the old number were re-checked.
- [ ] The number as published carries its counting condition inline.
