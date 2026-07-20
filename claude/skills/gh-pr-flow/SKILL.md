---
name: gh-pr-flow
description: Ship code through the PR-first GitHub flow on this node, including protected PRs that need a Seoseo-held jinon86 review or merge after fresh explicit user approval. Use when committing or pushing code, opening or merging a PR, resolving REVIEW_REQUIRED, or landing changes in jinwon-int repos. Enforces no direct main pushes, exact-head and green-check validation, independent review, secret-safe remote credential use, squash merge, and verified cleanup. Not for Wiki edits (use wiki-record).
---

# gh-pr-flow — PR-first GitHub flow

Use this for code changes that land in GitHub. Operational repos live under
`jinwon-int` where possible. Never push directly to `main`; use a branch and PR.
The normal local `gh` identity is `seoseo-ai`. For Wiki content use
`wiki-record` instead.

## GitHub transport policy

- Use local `git` and the authenticated `gh` CLI for every GitHub read and write.
- Do not use GitHub App, connector, MCP, or plugin tools unless the user
  explicitly requests that transport in the current task.
- If `gh` fails, report the error; do not automatically retry through a GitHub
  connector.
- Run `gh auth status` before the first authenticated GitHub operation when the
  current session has not already verified it.

## Procedure

1. Sync and branch:

   ```bash
   git switch main
   git pull --ff-only
   git switch -c <type>/<slug>
   ```

2. Stage only the intended files and commit with the required trailer:

   ```bash
   git add <file>...
   git commit -F - <<'EOF'
   <type>(<scope>): <imperative summary>

   <optional body — what and why>

   Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
   EOF
   ```

3. Push and open a PR against `main`:

   ```bash
   git push -u origin <branch>
   gh pr create --repo <owner/repo> --base main --head <branch> \
     --title "<type>(<scope>): <summary>" --body "<what / why / evidence>"
   ```

4. Inspect identity, review, exact head, checks, and merge state:

   ```bash
   gh pr view <n> --repo <owner/repo> \
     --json author,reviewRequests,reviewDecision,headRefOid,isDraft,mergeable,mergeStateStatus,statusCheckRollup
   ```

5. Resolve `REVIEW_REQUIRED` with an independent reviewer. Never use `--admin`
   merely to bypass branch protection.

   - A `jinon86`-authored PR uses `seoseo-ai` as reviewer when required.
   - A `seoseo-ai`-authored PR that requests `jinon86` may use the Seoseo-held
     `jinon86` GitHub session only after the user explicitly approves that exact
     credential use in the current conversation.
   - Old approvals, memory, environment state, or approval for another PR do
     not count. If approval is absent or ambiguous, stop and ask.
   - After approval, set the approval flag only on this one command:

     ```bash
     CCC_EXPLICIT_USER_APPROVAL=1 \
       ~/.claude/skills/gh-pr-flow/approve-via-seoseo.sh <owner/repo> <pr-number>
     ```

   The helper accepts only `jinwon-int/*`, verifies the remote actor is
   `jinon86`, and requires an open `main` PR authored by `seoseo-ai` with
   `jinon86` requested. It refuses self-review and returns only safe review
   status. The GitHub credential remains behind Seoseo's `gh` session boundary.

6. With required review and checks green, squash-merge normally:

   ```bash
   gh pr merge <n> --repo <owner/repo> --squash --delete-branch
   ```

   If local `seoseo-ai` lacks repository merge permission, use the exact-head
   Seoseo merge fallback below. Do not weaken branch protection.

7. Verify and clean up:

   ```bash
   gh pr view <n> --repo <owner/repo> --json state,mergedAt,mergeCommit
   git switch main
   git pull --ff-only
   git ls-remote --exit-code --heads origin <branch>
   ```

   A nonzero `ls-remote` result means the remote branch is absent, as expected.
   `git branch -d <branch>` may reject a squash-merged branch because squash
   does not preserve ancestry. Use `git branch -D <branch>` only after verifying
   the PR is merged, `main` contains the change, and the remote branch is gone.
   If the PR links an issue with a closing keyword, verify that issue is closed.

## Seoseo `jinon86` exact-head merge fallback

Use this only when all of the following are true:

- The operator explicitly approved merging this specific repository and PR in
  the current task. Approval does not carry to another PR or a changed head.
- The local identity cannot merge it, while Seoseo already has the authorized
  `jinon86` GitHub session.
- The PR is non-draft, targets `main`, is `MERGEABLE`/`CLEAN`, has all required
  reviews, and its exact head has passing GitHub checks or documented equivalent
  validation.

Capture the exact head locally, then call the fail-closed helper:

```bash
head_sha="$(gh pr view <n> --repo <owner/repo> --json headRefOid --jq .headRefOid)"
bash ~/.claude/skills/gh-pr-flow/merge-via-seoseo.sh \
  --repo <owner/repo> --pr <n> --expected-head "$head_sha" \
  --operator-approved
```

The helper verifies actor `jinon86`, re-reads the PR on Seoseo, requires the
same head SHA and clean merge state, rejects pending or failed checks, and uses
GitHub's merge API with the SHA precondition. It never uses an admin bypass. A
PR with no reported checks is allowed only when exact-head equivalent evidence
is already recorded and the operator is told that GitHub reported no checks.

Delete the contributor branch using the identity that owns it. If cleanup
permission is unavailable, report it rather than moving a credential.

## Security and merge rules

- Never push directly to `main`; always use a branch and PR.
- The PR author cannot approve their own PR. Keep author and reviewer identities
  independent.
- Only merge with green required checks and a mergeable state. Report failed or
  pending checks instead of forcing the merge.
- Never read, print, copy, persist, export, or re-login with the Seoseo token.
  Run `gh` on Seoseo so the credential stays there. Do not enable shell trace,
  switch the persistent local account, or put credentials in arguments,
  commits, PR text, logs, or memory.
- An approval flag records a fresh user decision; it does not grant standing
  authority. Set it for one approved invocation only. Review approval and merge
  approval are separate privileged writes unless the user's current instruction
  explicitly authorizes both for that exact PR.
- A merge instruction authorizes the merge, not token disclosure, credential
  transfer, release/publish, deploy, restart, migration, or another mutation.
