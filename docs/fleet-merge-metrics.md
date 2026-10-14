# Fleet merge-pipeline metrics (`scripts/fleet-metrics/fleet_merge_metrics.py`)

Read-only measurement of how PRs move through the fleet's GitHub merge pipeline
— the thing that actually bottlenecks agent-scale development here, as opposed
to git itself (Wiki decision [DOC-3932](https://wiki.seoyoon-family.com/#/pages/decisions/agent-scale-source-control-2026-10),
issue #2302). Numbers from it decide repository-setting changes such as merge-queue
batching, and verify them afterwards.

## What it measures (per repo, last N days)

| Column | Source | Meaning |
|---|---|---|
| 머지 PR, /일 | GraphQL `pullRequests(states: MERGED)` | landing volume |
| PR 사이클 중앙/p90 | `createdAt → mergedAt` | whole pipeline incl. review and approval waits |
| 큐 PR, 큐 대기 | timeline `ADDED_TO_MERGE_QUEUE` (last) → `mergedAt` | time spent inside the merge queue |
| 큐 그룹, 실효 배치 | `merge_group` runs' `gh-readonly-queue/<base>/pr-N-<sha>` refs grouped by sha; merged PRs ÷ groups | how many PRs one speculative run lands (1.0 = fully serialised) |
| 그룹 wall | min start → max end of a group's runs | wall-clock cost of one queue slot |
| 축출≈ | `REMOVED_FROM_MERGE_QUEUE` events − queued PRs | landing also emits a removal, so this is an estimate |
| CI run, CI 분, 실패율 | `/actions/runs` by `event` (`pull_request`, `merge_group`, `push`, …) | runner usage and retry cost |

## Usage

```bash
# default: 7 fleet repos, 30 days, writes merge-metrics-<KST stamp>.{json,md} in cwd
python3 scripts/fleet-metrics/fleet_merge_metrics.py
python3 scripts/fleet-metrics/fleet_merge_metrics.py --repos ccc-node,family-messenger --days 14 --out-dir /tmp
python3 scripts/fleet-metrics/fleet_merge_metrics.py --markdown-only
python3 scripts/fleet-metrics/fleet_merge_metrics_test.py        # offline unit tests
```

Needs an authenticated `gh` with read access to `jinwon-int`. It never writes to
GitHub. Budget: a few hundred REST pages and ~20–60 GraphQL pages per large repo;
one repo's API failure (e.g. GraphQL 502 on the wiki repo) is reported in its row
and does not stop the others.

## Caveats (also printed under the table)

- `/actions/runs` returns at most **1,000 runs per query**; busy repos show `≥` and
  their CI minutes are lower bounds. Use the billing API for exact minutes.
- Group refs name only the *last* PR of a speculative group, so batch size is the
  ratio merged PRs ÷ groups, not a per-group count.
- Approval wait (human) is not measured directly: cycle − queue − CI.

## Baseline and the batching experiment (2026-10-14)

30-day baseline (2026-09-14 → 10-14): fleet ≈ 26 merges/day excluding the wiki repo;
merge queues allowed batches of 5 but the **effective batch was ≈ 1.0** (ccc-node 348 PRs /
375 groups, queue wait median 8.5 min, group wall 7.9 min) because `min_entries_to_merge=1`
with a 0-minute wait starts a group the moment one PR arrives.

Change applied 2026-10-14 08:16 KST (owner approval; #2302): `min_entries_to_merge 2`,
`min_entries_to_merge_wait_minutes 3` on ccc-node, family-messenger and a2a-nexus
(a2a-nexus also `max_entries 1→5`). Verification on 2026-10-28 with
`--repos ccc-node,family-messenger,a2a-nexus --days 14`: keep if effective batch ≥ 1.3,
otherwise restore the backed-up rulesets.
