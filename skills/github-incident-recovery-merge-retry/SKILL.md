---
name: github-incident-recovery-merge-retry
description: Recover an authorized, reviewed pull request after GitHub transport or service failures. Verify checks and independent review on the exact head, distinguish queue admission from merge completion, and retry only classified transient failures within a fixed budget. Use when CI or merge operations fail because of GitHub infrastructure rather than a code or policy failure.
---

## Preconditions

Work within the user's existing authorization and the repository's normal
protection rules. Never use an administrator override, disable checks, or use
another transport to bypass a merge queue. Independent review must come from a
reviewer other than the author; the person submitting the merge may be the author.

Record the repository, PR number, PR node ID, intended base and full head SHA
(`EXACT_HEAD`). Confirm the PR is OPEN and not a draft. Capture one consistent
metadata snapshot, then re-read the head immediately before any write:

```bash
gh pr view <n> --repo <owner>/<repo> \
  --json id,state,isDraft,baseRefName,headRefOid,author,mergeable,mergeStateStatus,reviewDecision,latestReviews,statusCheckRollup
```

Proceed only when all these conditions hold:

- The head still equals `EXACT_HEAD`; the base and scope match the reviewed change.
- Required checks are present and passing for this head and the relevant event.
  A missing/empty check list is not a pass. Check the base branch's effective
  protection/rulesets when the expected check set is uncertain.
- Required independent approval is current and bound to this head. Check the
  latest effective review per reviewer, including its commit OID, author and
  state; a login plus `APPROVED` alone does not establish a head binding. Require
  the effective review decision to satisfy repository policy, with no unresolved
  blocking review. A head change requires fresh validation and review evidence.
- GitHub reports MERGEABLE and its protection gates permit the selected normal
  merge path. UNKNOWN needs another read; CONFLICTING/DIRTY needs source repair;
  BEHIND needs an appropriate base refresh and new CI; BLOCKED/UNSTABLE needs its
  actual failing gate resolved. Do not treat an expected hook as a passed gate.

Local `git merge-tree` can help diagnose a persistent UNKNOWN, but cannot replace
GitHub checks, reviews, authorization or the server's merge decision.

## Recover CI first

Check GitHub's component status and the actual failed run logs. A green status
page does not prove the particular request or run has recovered, and an outage
can coexist with a code failure. Do not assume a universal REST/GraphQL failure
ordering.

Only rerun a run whose full `headSha` equals `EXACT_HEAD` and whose failure was
classified as infrastructure-related. List candidates without rerunning them:

```bash
gh run list --repo <owner>/<repo> --branch <branch> \
  --json databaseId,conclusion,headSha \
  --jq ".[] | select(.conclusion==\"failure\" and .headSha==\"$EXACT_HEAD\") | .databaseId"
```

For a verified candidate, `gh run rerun <run-id> --repo <owner>/<repo> --failed`
retries failed jobs. Recheck the PR head before rerunning. If the same failure
returns after infrastructure recovery, diagnose it instead of repeatedly
rerunning. Use `gh-ci-wait` when handing off a promise to resume after CI;
foreground checks alone do not create a durable wait.

## Submit through the repository's merge path

For a required merge queue, use an enqueue request with the expected head. The
variables below are the previously verified PR node ID and full SHA:

```bash
gh api graphql \
  -f query='mutation($id:ID!,$head:GitObjectID!){enqueuePullRequest(input:{pullRequestId:$id,expectedHeadOid:$head}){mergeQueueEntry{id position}}}' \
  -f id="$PR_ID" -f head="$EXACT_HEAD"
```

Queue admission leaves the PR OPEN. Poll its queue/check state and eventual
merge result; a queue eviction is a failed attempt that needs diagnosis. Do not
supply `--delete-branch`, call direct REST merge, or convert a queue outage into
a bypass. A changed head invalidates the saved review target.

Where no queue is required, use the normal pinned merge:

```bash
gh pr merge <n> --repo <owner>/<repo> --squash \
  --match-head-commit "$EXACT_HEAD"
```

A direct REST merge is an alternative only where direct merging is allowed and
the original failure was classified as transient. Re-read state and all gates
first; do not chain it unconditionally with `||`:

```bash
gh api -X PUT repos/<owner>/<repo>/pulls/<n>/merge \
  -f merge_method=squash -f sha="$EXACT_HEAD"
```

A successful response, enablement of auto-merge, or an enqueue result is not the
final evidence. Read back the actual merge as described below.

## Bound retries and preserve uncertain outcomes

Use a fixed overall deadline and attempt cap, for example 40 attempts spaced
three minutes apart with a two-hour deadline. Each request needs its own shorter
timeout; neither retrying nor re-authentication extends the overall budget.
These are an example retry budget, not defaults enforced by this skill.

Before each attempt, read back state and the head. If an earlier write may have
succeeded, reconcile it first: inspect the actual merge or existing queue entry
before issuing another write. CLOSED without a merge is terminal, not a reason
to try another merge transport.

Apply this classification to **reads as well as writes**:

| Outcome | Action |
| --- | --- |
| Explicit connection timeout/reset, or HTTP 5xx | Reconcile any uncertain write; retry only within the remaining budget. |
| 401/403 | Stop and resolve authentication/permission or the documented rate-limit condition; no blind retry. |
| 404/405/409/422 | Stop and read back/classify not-found, mergeability, head drift, validation or already-completed state. |
| 429 | Respect the documented retry condition and remaining budget only after explicit classification. |
| Other HTTP status, GraphQL error, malformed output or unknown CLI failure | Stop and diagnose; do not label a catch-all branch transient. |
| MERGED | Verify and record completion; do not retry a merge to prove it happened. |

A detached watcher must implement these same checks, queue/direct separation and
bounded request/deadline behavior. Do not use a generic shell loop that retries
every failure or declares success solely from the last command's exit code.

Before detaching, validate `gh auth status --hostname github.com` **inside the
same execution context** the watcher will use: same UID, HOME, GH_CONFIG_DIR,
PATH and secret-loading mechanism, with ambient GH_TOKEN/GITHUB_TOKEN absent
unless intentionally provided there. An interactive auth check under inherited
environment does not prove a future service can authenticate. Prefer the
existing protected gh credential store or an existing managed service's
protected secret loader. Do not copy tokens, place them in argv/`--setenv`, or
source an untrusted file. Fail the preflight without starting retries when the
watcher context cannot authenticate. Store diagnostic records in a protected,
owner-only location; emit status codes and IDs without tokens, response bodies
or credential-bearing URLs.

## Verify completion

```bash
gh pr view <n> --repo <owner>/<repo> \
  --json state,mergedAt,mergeCommit,headRefOid \
  --jq '{state,mergedAt,oid:.mergeCommit.oid,headRefOid}'
```

Require `state == "MERGED"`, a nonempty merge time and merge commit. Tie the
merge to the reviewed head and the retained submission/queue evidence; a PR
already merged by another actor is not proof this attempt merged its saved SHA.
Fetch the intended base and verify the recorded merge commit is an ancestor.

Branch cleanup is separate. Neither the direct REST call nor the enqueue above
requests branch deletion; repository auto-delete settings or other actors may
remove it. Read its actual state and apply only authorized cleanup. Branch
presence/absence is not merge-success evidence. A working-state checkpoint
preserves continuity; it is not proof of CI or merge completion.

## Re-verify the tooling

Check `gh pr view --help`, `gh run list --help` and `gh pr merge --help` on the
installed build, and inspect a read-only JSON response for the fields you use.
The CLI uses `state`, `mergedAt` and `mergeCommit`; do not assume a `merged` JSON
field. Validate command examples with temporary git repositories and mocked
API/CLI responses, never by running real approval or merge writes as a test.

Related skills: `gh-pr-flow`, `gh-ci-wait`,
`github-merge-state-conflict-diagnosis`.
