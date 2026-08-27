---
name: fleet-task-handoff-with-constraints
description: Hand off unfinished multi-node work by classifying each item's approval gate, recording where it may NOT run (observation nodes under measurement), verifying every cited metric's counting method, and naming prior errors so they aren't rediscovered. Use when transferring work mid-pilot, at session end with items outstanding, or when a task must move to a different node.
---

# When to Use

- Work is unfinished and someone (another node, another session, another operator)
  must pick it up.
- A pilot or measurement window is running on some nodes while work continues.
- Remaining items have *different* approval gates — some you can delegate, some only
  the owner can do, some are simply not actionable yet.

# Core insight

A handoff fails in three specific ways, and each has a specific countermeasure:

| Failure | Countermeasure |
|---|---|
| Successor does the work **on the wrong node** and destroys the measurement | Node constraint, with the mechanism spelled out |
| Successor **waits** on an item nobody can do without the owner | Approval classification |
| Successor **re-derives** a number, gets a different one, and re-decides | Metric method, carried inline |

The expensive one is the first. **Work is itself an input to the metric.** On a node
whose inflow rate is being measured, running a session *generates* the very facts
being counted — so the successor's diligence is indistinguishable from the signal.
That is invisible to the successor unless you write it down.

# Procedure

1. **Enumerate outstanding items** — one line each, numbered, phrased as an action.

2. **Classify each by gate** — exactly one label per item:
   - **owner-only** — needs a secret, an OAuth identity, or a fresh approval decision
     the successor cannot make (per `Fresh Approval Required`).
   - **delegatable** — no blocker; can start now.
   - **time-gated** — blocked on a window or external event; record *when* it unblocks
     and *what* unblocks it.

3. **Attach node constraints to every delegatable item.** Do not write "avoid node X" —
   write the mechanism, or it reads as arbitrary and gets ignored:

   > ③④ must run on a node other than `yukson`. `yukson` and `nosuk` are in the
   > 08-27~28 APPLY pilot and their **inflow rate** is the observed variable. A
   > session on `yukson` runs distill, which writes new review-queue facts — the
   > work would be counted as pilot inflow and the measurement becomes unusable.

4. **Verify every metric before it enters the document.** Run the counting method
   check (see the `metric-counting-method-verification` skill) and carry each figure
   with its predicate inline — `968 (valid_to IS NULL AND review=1)`.

5. **Lead with prior errors, not with status.** List each mistake made during the work,
   the correction, and the evidence. This section goes **first** — it is the only part
   the successor cannot reconstruct from the repo, and it is worth more than the
   status table.

6. **List the DO-NOT-COPY traps** — only when handing off between *phases* of the
   same project. The expensive phase-handoff failure is a successor copying a
   working pattern from the previous phase into one with different semantics. For
   each such pattern write three things, not one:
   - what the prior pattern was, and why it worked *there*;
   - the **specific failure mechanism** if it is copied here (a changed signature,
     a one-shot trigger where this phase is level-triggered, a check that needs a
     real resource where the prior one could stub it);
   - the evidence — a commit SHA, a signature, or the failing result.

   "Be careful, this phase is different" is not a trap entry. Separate
   show-stoppers from optimizations, and label what was *observed* in prior work
   versus what you are *predicting* for this phase — that blur is what makes a
   successor trust a guess.

7. **Mark what you could not verify, explicitly.** Where your own conclusion rests
   on interpretation, say so in the first person and say why you cannot settle it
   — e.g. "I believe this is the right reading, but I cannot verify it
   independently." A confident handoff with a silent guess inside it is worse than
   one that flags the guess, because the successor inherits the confidence too.

8. **Write it to a durable file, then link it.** A handoff that exists only in chat
   dies at the next compaction. Write to a path, then reference that path from the
   issue comment.

   **Check the size before posting.** A GitHub comment caps at roughly 65,000
   characters, and an oversized post can be rejected or silently truncated —
   losing the tail, which is where the next-move section lives. If the document
   is near the limit, keep the file as the source of truth and post a short index
   plus the link rather than the full body.

9. **Close with a single "next move"** — the one action the successor should take
   first, with its node and its command. Ambiguity at step 1 stalls the whole handoff.

**If the handoff will be reviewed:** the reviewer must not have participated in
the earlier phases. A reviewer who was there reconstructs the missing context
from memory and cannot see what the document fails to say — which is the only
thing the review is for.

# Safety

- **Never hand off delegatable work during an active measurement without the node
  constraint.** The successor will comply if they know; they cannot infer it.
- Do not classify an item as delegatable to avoid the awkwardness of an owner-only
  blocker — a mislabeled item burns the successor's time and returns unfinished.
- Verify metrics live at handoff time; a figure inherited from earlier in the same
  session is not independently verified.
- Never inline secrets — record their location and handling rule only.

# Verification

- [ ] Every item carries exactly one gate label.
- [ ] Every delegatable item states where it may run **and why** (mechanism, not fiat).
- [ ] Every time-gated item names its unblock date and its unblock condition.
- [ ] Every metric appears with its counting condition.
- [ ] Prior errors appear before the status table.
- [ ] The document is on disk at a stated path and linked from the tracking issue.
- [ ] A successor reading only this document knows their first command and its node.
