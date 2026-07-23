---
name: gh-pr-flow
description: Validate, independently review, and normally squash-merge protected GitHub pull requests. Use when a PR must pass exact-head, green-check, author/reviewer separation, or required-review gates; when a jinon86-authored PR needs approval from the owner-only seoseo-ai gh config held on Seoseo; or when landing changes without weakening branch protection.
---

# GitHub PR Flow

Use local `git` and authenticated `gh` for GitHub reads and writes. Never push
directly to `main`, approve your own PR, use `--admin` merely to bypass
protection, or move a credential between nodes.

## Normal flow

1. Record the PR's full `headRefOid`. Require an open, non-draft PR against the
   intended base, a mergeable state, and no pending or failed required checks.
2. Confirm the PR author and current actor. Request a different write-capable
   reviewer when approval is required.
3. After approval, re-read the exact head, review decision, and checks. Squash
   merge normally:

   ```bash
   gh pr merge NUMBER --repo OWNER/REPO --squash --delete-branch
   ```

4. Verify the merged commit and remote branch deletion before removing a local
   squash-merged branch.

## Seoseo-held seoseo-ai review

Use this only for a `jinon86`-authored PR in `jinwon-int/*` after the user
explicitly approves use of the Seoseo-held `seoseo-ai` credential for that
exact repository, PR, and head:

```bash
CCC_EXPLICIT_USER_APPROVAL=1 \
  bash "${CODEX_HOME:-$HOME/.codex}/skills/gh-pr-flow/scripts/approve-via-seoseo-ai.sh" \
    --repo jinwon-int/REPO --pr NUMBER --expected-head FULL_40_CHAR_SHA \
    --ssh-target seoseo --operator-approved
```

The helper uses only Seoseo's owner-only
`/root/.config/gh-seoseo-ai/hosts.yml`, beneath a root-owned config directory
that is not writable by group or other, without switching the default account.
It verifies actor `seoseo-ai`, repository write permission, author separation,
requested-reviewer state, exact head, mergeability, and green checks before
submitting a commit-bound approval.

## Security boundary

- Require fresh explicit approval for every helper invocation. Approval does
  not carry to another repository, PR, or changed head.
- Never read, print, copy, export, re-login, or place the token in arguments.
- Keep shell tracing disabled. Return only body-free gate results.
- Stop on credential owner/mode drift, wrong actor, self-review, head drift,
  missing review request, missing checks, or non-green checks.
- Review approval and merge are separate writes unless the current user
  instruction explicitly authorizes both for that exact PR.
