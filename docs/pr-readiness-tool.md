# PR readiness tool — pre-merge lookup snapshot (CLI + `family-ops` MCP, #1694 item 3)

One read-only call aggregates what a merge decision scans for, for one pull
request of one repository, through the node's authenticated `gh`. Same
structure as `node_status`/`task_status` (see `docs/node-status-tool.md`,
`docs/task-status-tool.md`): a stdlib-only module
(`bridge/core/pr_readiness.py`), the JSON CLI `scripts/ccc-pr-readiness.py`,
and the `pr_readiness` tool on the existing `family-ops` MCP server — one
tool per roadmap item.

## Sections

| Section | Source | Content |
|---|---|---|
| `pull_request` | `gh pr view` | head sha/branch, base, state/draft, `mergeable`/`mergeStateStatus`, review decision, review requests |
| `ci` | `statusCheckRollup` at the exact head | counted with the **gh-pr-flow relay gate rule**: SUCCESS/NEUTRAL/SKIPPED acceptable; not COMPLETED, or any other conclusion, bad; zero checks is its own `no_checks` verdict. Failed/pending names listed. |
| `reviews` | latest review per reviewer (+ its `commit.oid`) | approvals split into **non-author head-matched** vs stale/unknown-commit; changes requested; other latest states |
| `threads` | GraphQL `reviewThreads` (first 100) | unresolved count, outdated flagged, `partial` when totalCount exceeds the sample |

Every section reports `status: ok|unknown` with observation latency; a failed
or unparseable `gh` call is section-scoped `unknown` and never fabricates
values. If `pull_request` fails, the derived `ci`/`reviews` sections become
`unknown` too; `threads` is independent.

## Readiness snapshot

`readiness` is an informational aggregation (`verdict: likely_ready|blocked`
plus `reasons[]`, `informational_only: true`). Reasons include `not_open`,
`draft`, `not_mergeable`, `mergeable_computing`, `merge_state:*`, `ci:*`,
`review_decision:*`, `no_non_author_head_matched_approval`,
`approval_stale_or_head_unknown`, `changes_requested_open`,
`unresolved_review_threads`, and the `*_unknown` markers for failed sections.

It mirrors the gate — it does not replace it. The gh-pr-flow relay approval
(`approve-via-relay.sh`) and merge-time re-verification keep every rule they
ever had; this snapshot shares their counting method (relay CI rule verbatim)
so a lookup agrees with the gate that later runs.

## CLI and MCP

```bash
python3 scripts/ccc-pr-readiness.py --repo OWNER/REPO --pr NUMBER
```

MCP: `mcp__family-ops__pr_readiness` (`repo`, `pr` required) on the
`family-ops` server — same call-time node-policy gate (external/shared
denied) as the other tools.

## Principles

- Strictly read-only: no approvals, merges, comment posts, or state changes —
  `gh` is only invoked for reads (`pr view`, `api graphql`).
- The result is owner-context operational data and is served only through
  the node policy gate; the server logs counts to stderr, never bodies.
- `matches_head` is `null` (unknown) when a review carries no commit info —
  such approvals are listed under stale/unknown, never silently counted.

## Scope notes

- Review-thread sampling is capped at the first 100 threads (`partial: true`
  beyond that). Cross-repo/ssh aggregation is not in this item.
- Test seam: `CCC_PR_READINESS_GH` (space-separated `gh` command override)
  plus the injected runner in unit tests.
