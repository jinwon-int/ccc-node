---
name: gh-pr-flow
description: Validate, independently review, and normally squash-merge protected GitHub pull requests. Use when a PR must pass exact-head, green-check, author/reviewer separation, or required-review gates; when a jinon86- or seoseo-ai-authored PR needs approval from the other relay-held account; or when landing changes without weakening branch protection.
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

## Relay-held cross-account review

Use the allowlisted review profile matching the PR author. Both directions
require fresh explicit approval for the exact repository, PR, and head:

| PR author | Review profile | Expected reviewer | Remote gh config |
| --- | --- | --- | --- |
| `jinon86` | `seoseo-ai` | `seoseo-ai` | isolated root-owned profile config |
| `seoseo-ai` | `jinon86` | `jinon86` | root default gh config |

`relay` below is the SSH alias of the credential-holding relay node in your
fleet; pass `--ssh-target` explicitly or export `CCC_RELAY_SSH_TARGET`:

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
green checks before submitting a commit-bound approval. Compatibility wrappers
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
