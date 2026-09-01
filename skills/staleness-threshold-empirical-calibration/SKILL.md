---
name: staleness-threshold-empirical-calibration
description: Choose a threshold for a staleness/freshness check so it neither sits dead nor invents a signal — confirm what the metric actually measures, use the corpus distribution to detect dead thresholds, and derive the number itself from the governing convention or policy. Use when adding a check like "flag if unchanged for >N days", when an existing freshness check never fires, or when a reviewer asks why N was chosen.
---

## When to Use

You are adding or tuning a staleness detector — aged document reviews, snapshot
freshness, metadata age, unchanged-since-date alerts — and must pick the `N` in
"flag if older than N". Also use when an existing check has never fired and you
need to decide whether that means "healthy" or "broken".

## Procedure

1. **Confirm what the metric actually measures — read the defining document, not the field name.**
   Field names lie by omission. `verified`, `updated`, `checked`, `last_seen`
   all sound like freshness but can mean very different things:

   | Meaning | An old value implies |
   |---|---|
   | last re-verified against reality | the claim may now be wrong |
   | last edited | nothing — a stable page has no reason to change |
   | last accessed / synced | the *reader* went quiet, not the data |

   Find the convention/schema that defines the field and quote it. If it means
   "last edited", a staleness check over it is a **review-cadence nudge**, not a
   correctness signal, and must be described that way.

   *This is the step most often skipped, and skipping it makes every later step
   meaningless.* A 2026-08-27 check was built assuming `verified` meant
   "re-verified"; the convention actually said "last edited". The threshold work
   downstream was calibrating the wrong quantity.

2. **Pull the corpus distribution.** For every item the check will watch,
   collect the metric. Report count, median, p90, and the **observed maximum**.

3. **Use the distribution for one purpose: detecting a dead threshold.**
   If the candidate `N` exceeds the observed maximum, the check can never fire.
   That is worse than no check — "nothing stale found" reads as "the check
   works", so it manufactures confidence while watching nothing.

   Print the firing count at several candidate thresholds. A row that is 0 at
   every plausible `N` down to the observed max means the check is dead.

4. **Derive the number from the governing convention, not from the distribution.**
   Ask: what cadence does the policy, runbook, or schema imply? A quarterly
   review cycle gives 90 days. A daily refresh lane with a 1-day freshness
   bound gives ~7 days (a week of silence means the lane stopped).

   **Do not** compute the threshold from the data — `max_observed × 1.2`,
   `p90`, and friends pick a number engineered to fire. That is inventing a
   signal: the check will produce findings, but no one can say what a finding
   *means*, and nobody can act on it. If no convention exists, say so and
   propose one rather than fabricating a constant.

   Sanity-check the two against each other. If the convention-derived `N` is far
   below the observed max, items are already overdue — report that. If it is far
   above, keep it but state that it is a tripwire (step 5).

5. **Distinguish a tripwire from a dead check — both report zero.**
   They are not the same thing and the difference is *why*:

   - **Tripwire**: `N` is grounded in the convention, and zero findings means
     the corpus genuinely complies. Reachable — lowering `N` toward the observed
     max produces findings. Correct outcome.
   - **Dead**: `N` sits above anything the corpus can reach. Unreachable at any
     realistic value. Delete or retune.

   Demonstrate reachability explicitly: run once at a lower `N` and show the
   count rising. An unreachable threshold that was never probed is
   indistinguishable from a healthy one.

6. **Record the evidence next to the constant.** In the code comment or runbook,
   state what the metric means (with the quote), the corpus stats at calibration
   time, and which convention produced `N`. Without this the next maintainer
   re-derives it by guessing, or "fixes" a tripwire that was working.

7. **Re-check when the corpus shifts.** New usage patterns move the
   distribution. Re-run steps 2–3; a threshold that was a tripwire can go dead
   as the corpus gets healthier.

## Safety

- **Never read "check never fires" as "check works."** Silence has two causes —
  compliance and blindness — with opposite responses. Step 5 separates them.
- **Never let a check's design outrun what the metric can support.** If the
  field means "last edited", no threshold turns it into a correctness signal;
  fix the description instead of the number.
- Only flag items the system can actually refresh. Flagging entries that nothing
  updates produces permanent, unfixable findings, and a report with unfixable
  rows teaches readers to skip the whole report. Find the discriminator that
  separates refreshable from manual entries and check only the former.

## Verification

- The first run's finding count matches what the distribution predicted.
- A deliberately lowered `N` produces findings — proves reachability.
- The metric's meaning is quoted from its defining document in the code or docs.
- Every finding is on an item something can actually refresh.
