# Skill intake lineage report (#2641)

`scripts/ccc-skill-lineage-report.py` is a read-only report over the
skill-promotion ledger (`~/.claude/state/skill-promotion/ledger.jsonl` on the
curator node). It replaces the verdict-level "revise ratio" that the
#2239 programme tracked with lineage-level outcome metrics. It never writes
anything and prints counters, PR numbers, node names and timestamps only.

## Why lineages

The #2239 second verdict (2026-11-02 22:45 KST) measured a 7-day verdict
revise share of 57.7% (56/97) against a <40% target, and found the structure
behind it: 48 of the 56 revises were **first** reviews and 35 of the 41
approves were **republished** PRs. The pipeline converges as

    first review: revise  →  a2a-revise-result (auto-fix, republish)  →  review again: approve

so the share of `revise` among verdicts measures how many rounds a skill takes,
not whether it lands. The owner closed #2239 as partially achieved and moved
the quality metric to #2641:

| metric | definition |
|---|---|
| final approve rate (decided) | lineages whose latest consumed verdict is `approve` ÷ lineages that are approved, rejected or dropped |
| final approve rate (all) | same numerator ÷ every lineage in the cohort (in-progress ones count against it) |
| mean rounds | consumed verdicts per lineage (all / decided / approved) |
| first-review approve rate | lineages whose first consumed verdict was already `approve` |

## How a lineage is built

| ledger row | used as |
|---|---|
| `a2a-verdict` | a round; PR and author node parsed from `task_id` = `skills_intake_review-pr<N>-<node>-<UTC stamp>`; only `status=consumed` rows count, `malformed` / `infra_failed` are reported in `excluded` |
| `a2a-intake-superseded` | link `pr` → `superseded_by` |
| `a2a-revise-result`, `status=republished` | link `pr` → PR number in `new_pr_url` |
| `a2a-revise-result`, other statuses | outcome evidence after the latest `revise` verdict |
| `a2a-arbitration` | flag only (`arbitrated`, `arbitration_triggers`) |

PRs are grouped into connected components under the two link kinds; the
smallest PR number names the lineage (`root_pr`). The author node is the node
of the first consumed verdict.

Outcome from the **latest consumed verdict** of the lineage (the same
"latest verdict decides" rule `ccc_skill_promotion_lineage_state_test.py` pins
for the dispatcher):

| outcome | rule |
|---|---|
| `approved` | latest verdict `approve` |
| `rejected` | latest verdict `reject` |
| `dropped` | latest verdict `revise` and the newest revise-result at/after it is `invalid`, `drop-recommended` or `publish-failed` |
| `in_progress` | latest verdict `revise` and nothing terminal yet (`failed` / `infra_failed` revise results are retried; open arbitrations stay here) |

## Cohort window

A lineage belongs to the window when its **first consumed verdict** is in
`[--since, --until)`. Its outcome is read from the whole ledger, so a cohort is
judged by what eventually happened to it; pass a ledger truncated at a date to
get an as-of view. Default window: 14 days ending now (`--window-days`).

`legacy_verdict_window` applies the old definition (verdict rows with `ts` in
the window, `status=consumed` denominator) so both numbers sit side by side.

## Usage

```bash
# on the curator node (ledger owner), text summary of the last 14 days
scripts/ccc-skill-lineage-report.py

# explicit cohort, every lineage listed
scripts/ccc-skill-lineage-report.py --since 2026-10-19T13:45:00Z --until 2026-11-02T13:45:00Z --lineages

# from another node without copying the ledger: stream it over ssh, read stdin
ssh <curator-node> cat ~/.claude/state/skill-promotion/ledger.jsonl | scripts/ccc-skill-lineage-report.py - --json
```

`CCC_SKILL_PROMOTION_LEDGER` overrides the default path. Exit codes: 0 report
printed, 64 bad arguments, 66 ledger not found.

Tests: `scripts/ccc-skill-lineage-report.test.sh` (hermetic; synthetic ledger
rows shaped like the 2026-11-03 curator-node sample).
