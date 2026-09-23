---
name: gh-pr-flow
description: Validate, independently review, and normally squash-merge protected GitHub pull requests. Use when a PR must pass exact-head, green-check, author/reviewer separation, or required-review gates; when a jinon86- or seoseo-ai-authored PR needs approval from the other relay-held account; or when landing changes without weakening branch protection.
---

# GitHub PR Flow

Use local `git` and authenticated `gh` for GitHub reads and writes. Never push
directly to `main`, approve your own PR, use `--admin` merely to bypass
protection, or move a credential between nodes.

## Issue claim before implementing

Before creating a branch (or dispatching an A2A patch lane) for a GitHub issue,
apply the fleet issue-claim rule (Family Wiki DOC-3508,
`pages/rules/github-issue-claim`). Parallel sessions share one GitHub account,
so the account name is not an identity.

- Read the issue first. An open PR that references it, or an issue-claim or A2A
  `Start` comment from the last 3 hours, means the issue is already taken: do
  not start. Coordinate on that PR or issue, pick other work, or ask the owner.
- Otherwise post a claim comment before any code change, with a machine-readable
  first line and a one-line human summary:
  `<!-- issue-claim:v1 node=<node> session=<session-or-task-id> state=active -->`
  followed by `claim: <node> (<session>) — scope: <one line> — PR ETA: <KST>`.
- Filing an issue is not a claim. Opening the PR supersedes the claim; when
  abandoning, post `state=released`. A claim with no PR or update for 3 hours
  has expired; take it over with `state=takeover prev=<previous session>`.
- To replace a claimed issue or open PR with a broader one, comment on it
  first with the reason. Never silently open and merge a replacement.

## Normal flow

1. Record the PR's full `headRefOid`. Require an open, non-draft PR against the
   intended base, a mergeable state, and no pending or failed required checks.
2. Confirm the PR author and current actor. Request a different write-capable
   reviewer when approval is required. Under `require_last_push_approval`
   (`jinwon-int/ccc-node` `main`) the last pusher is disqualified too, so
   pushing a fix to the other account's PR can leave nobody able to approve it
   — and force-pushing that commit away does not help, because the rule keys on
   the pusher, not the commits. Prefer a review comment, or carry the fix in a
   PR you author. Check before pushing:

   ```bash
   gh api repos/<owner>/<repo>/branches/main/protection \
     --jq '.required_pull_request_reviews.require_last_push_approval'
   ```
3. After approval, re-read the exact head, review decision, and checks. Squash
   merge normally — except on `jinwon-int/ccc-node` `main`, which runs a merge
   queue (see below):

   ```bash
   gh pr merge NUMBER --repo OWNER/REPO --squash --delete-branch
   ```

   **Merge queue on `ccc-node` `main` (since 2026-09-13), `fleet-skills` `main`
   (since 2026-09-17), and `piri` `main` (observed 2026-09-21):** direct merge
   is refused ("the merge strategy for main is set by the merge queue").
   Treat the list as open — it is a per-repo ruleset, and `piri` surfaced only
   when a merge failed mid-batch. Check first:

   ```bash
   gh api repos/OWNER/REPO/rulesets --jq '.[].id' | while read -r id; do
     gh api repos/OWNER/REPO/rulesets/"$id" --jq '[.rules[].type]|join(",")'
   done
   ```

   Enqueue instead, then poll until `state` is `MERGED` (the queue evicts on
   failing group checks; a new head needs fresh approval before re-enqueueing):

   ```bash
   pr_id="$(gh pr view NUMBER --repo OWNER/REPO --json id --jq .id)"
   gh api graphql -f query='mutation($id:ID!){enqueuePullRequest(input:{pullRequestId:$id}){clientMutationId}}' -f id="$pr_id"
   gh pr view NUMBER --repo OWNER/REPO --json state,mergeStateStatus
   ```

   Enabling a queue on a further repo has a prerequisite: every *required*
   check's workflow must also trigger on `merge_group`, or the group never
   reports and enqueued PRs wait forever.

4. Verify the merged commit and remote branch deletion before removing a local
   squash-merged branch.

   A queue merge does **not** clean up the head branch, and `gh pr merge -d` is
   refused on a queued repo, so cleanup falls entirely to the repo's
   `delete_branch_on_merge` — off on `ccc-node`, on for `piri`/`fleet-skills`.
   The same flow therefore self-cleans on one repo and leaks branches on
   another; `ccc-node` carries 95 remote heads as of 2026-09-21. Check and
   delete explicitly:

   ```bash
   git ls-remote --heads https://github.com/OWNER/REPO BRANCH | grep -q . \
     && gh api -X DELETE repos/OWNER/REPO/git/refs/heads/BRANCH
   ```

Under strict up-to-date protection, merging one PR flips its siblings to
`BEHIND`. Refresh via REST (older `gh` builds lack `gh pr update-branch`):
`gh api -X PUT repos/OWNER/REPO/pulls/NUMBER/update-branch`, wait for CI on
the new head, then re-run the relay approval — it is commit-bound to
`--expected-head`.

## Relay-held cross-account review

Use the allowlisted review profile matching the PR author. Both directions
require fresh explicit approval for the exact repository, PR, and head:

| PR author | Review profile | Expected reviewer | Remote gh config |
| --- | --- | --- | --- |
| `jinon86` | `seoseo-ai` | `seoseo-ai` | isolated root-owned profile config |
| `seoseo-ai` | `jinon86` | `jinon86` | root default gh config |

`relay` below is the SSH alias of the credential-holding relay node in your
fleet and the helper default; a node without that alias must pass
`--ssh-target <relay-host-alias>` explicitly or export `CCC_RELAY_SSH_TARGET`
(observed 2026-09-05 on a node whose ssh config names the relay node by its
own hostname alias):

```bash
CCC_EXPLICIT_USER_APPROVAL=1 \
  bash "${CODEX_HOME:-$HOME/.codex}/skills/gh-pr-flow/scripts/approve-via-relay.sh" \
    --review-profile seoseo-ai \
    --repo jinwon-int/REPO --pr NUMBER --expected-head FULL_40_CHAR_SHA \
    --ssh-target relay --operator-approved

CCC_EXPLICIT_USER_APPROVAL=1 \
  bash "${CODEX_HOME:-$HOME/.codex}/skills/gh-pr-flow/scripts/approve-via-relay.sh" \
    --review-profile jinon86 \
    --repo jinwon-int/REPO --pr NUMBER --expected-head FULL_40_CHAR_SHA \
    --ssh-target relay --operator-approved
```

The helper maps each profile to a fixed actor, opposite author, and gh config.
It verifies the root-owned credential boundary, repository write permission,
author separation, requested-reviewer state, exact head, mergeability, and
green checks before submitting a commit-bound approval. The helper is
idempotent per actor and exact head: a re-run reuses an already-recorded
exact-head approving review instead of posting a duplicate, and success is
judged from that recorded review — `reviewDecision` reads `null` on repos
whose branch protection requires zero approving reviews (#1714), so only an
explicit `CHANGES_REQUESTED` fails verification. Compatibility wrappers
`approve-via-seoseo-ai.sh` and `approve-via-jinon86.sh` select their named
profiles but cannot override them.

## Security boundary

- Require fresh explicit approval for every helper invocation and review
  profile. Approval does not carry to another repository, PR, changed head, or
  opposite credential.
- Never read, print, copy, export, re-login, or place the token in arguments.
- Keep shell tracing disabled. Return only body-free gate results.
- Stop on credential owner/mode drift, wrong actor, self-review, head drift,
  missing review request, missing checks, or non-green checks.
- Review approval and merge are separate writes unless the current user
  instruction explicitly authorizes both for that exact PR.
