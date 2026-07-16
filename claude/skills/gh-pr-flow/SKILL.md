---
name: gh-pr-flow
description: Ship code through the PR-first GitHub flow on this node, including protected merges that need the Seoseo-held jinon86 reviewer credential after fresh explicit user approval. Use when committing or pushing code, opening or merging a PR, resolving REVIEW_REQUIRED, or landing a change in jinwon-int repos. Enforces no direct main pushes, green checks, independent review, secret-safe credential handling, squash merge, and verified branch cleanup. Not for Wiki edits (use wiki-record).
---

# gh-pr-flow — PR-first GitHub flow

Use this for code changes that land in GitHub. Operational repos live under
`jinwon-int` where possible. Never push directly to `main`; use a branch and PR.
The normal local `gh` identity is `seoseo-ai`. For Wiki content use
`wiki-record` instead.

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

3. Push the branch and open a PR against `main`:

   ```bash
   git push -u origin <branch>
   gh pr create --repo <owner/repo> --base main --head <branch> \
     --title "<type>(<scope>): <summary>" --body "<what / why / evidence>"
   ```

4. Inspect the author, requested reviewers, decision, checks, and merge state:

   ```bash
   gh pr view <n> --repo <owner/repo> \
     --json author,reviewRequests,reviewDecision,mergeable,mergeStateStatus,statusCheckRollup
   ```

5. If checks are green and the PR is mergeable, squash-merge normally:

   ```bash
   gh pr merge <n> --repo <owner/repo> --squash --delete-branch
   ```

6. If GitHub reports `REVIEW_REQUIRED`, obtain an independent review. Never use
   `--admin` merely to bypass the rule.

   - A `jinon86`-authored PR uses `seoseo-ai` as reviewer when required.
   - A `seoseo-ai`-authored PR that specifically requests `jinon86` may use the
     Seoseo-held `jinon86` credential only after the user explicitly approves
     that exact credential use in the current conversation.
   - Old approvals, memory, environment state, or approval for another PR do
     not count. If approval is absent or ambiguous, stop and ask.
   - After approval, invoke the bundled helper with the approval flag set only
     on that command:

     ```bash
     CCC_EXPLICIT_USER_APPROVAL=1 \
       ~/.claude/skills/gh-pr-flow/approve-via-seoseo.sh <owner/repo> <pr-number>
     ```

   The helper accepts only `jinwon-int/*`, verifies an open `main` PR authored
   by `seoseo-ai` with `jinon86` requested, refuses self-review, and keeps the
   token inside the Seoseo SSH process. It returns only safe review status.
   Retry the ordinary merge command after approval. If auto-merge is disabled
   or another protection blocks it, report the blocker; do not force it.

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

## Security and merge rules

- Never push directly to `main`; always use a branch and PR.
- The PR author cannot approve their own PR. Keep author and reviewer identities
  independent.
- Only merge with green required checks and a mergeable state. Report failed or
  pending checks instead of forcing the merge.
- Never print, copy, persist, or locally retrieve the Seoseo token. In particular,
  do not run `gh auth token --user jinon86` outside the helper, enable shell trace,
  switch the persistent local `gh` account, or place credentials in arguments,
  commits, PR text, logs, or memory.
- The helper's approval flag records a fresh user decision; it does not grant
  standing authority. Set it inline for one approved invocation only.
- Releases, secrets, migrations, and any admin bypass require their own fresh
  approval even when the PR flow itself is already approved.
