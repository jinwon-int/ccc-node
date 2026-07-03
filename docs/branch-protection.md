# Branch protection and CODEOWNERS policy

`main` is protected by GitHub branch protection. This repository intentionally
keeps the source-controlled ownership file separate from live branch-protection
settings so protection changes stay auditable and reversible.

## Current code-owner model

- `.github/CODEOWNERS` covers the whole repository with both current
  write-capable maintainers: `@jinon86` and `@seoseo-ai`.
- Listing both accounts lets the account that did **not** author or push the PR
  satisfy a future code-owner review requirement.
- Do not raise `required_approving_review_count` above `1` until a third
  write-capable reviewer or team exists. With only two maintainers, a count of
  `2` can make PRs authored by either maintainer impossible to merge without an
  additional reviewer, because the PR author's own approval does not satisfy the
  required independent review.

## Approval boundary

`SECURITY.md` defines repository visibility, ownership, branch-protection, or ruleset changes as hard safety-boundary operations. Live branch-protection
settings therefore require explicit operator approval that names the exact
action, target, and rollback/no-op boundary before running GitHub API updates.

This source-only PR prepares CODEOWNERS and documents the safe operating model;
it does not mutate live branch protection.

## Safe future setting change

After explicit operator approval, the low-risk next live change is:

- target: `jinwon-int/ccc-node` branch `main`
- action: set `required_pull_request_reviews.require_code_owner_reviews=true`
- no-op boundary: preserve current required status checks, strict up-to-date
  branches, admin enforcement, stale-review dismissal, last-push approval, and
  `required_approving_review_count=1`
- rollback: set `require_code_owner_reviews=false`

Do **not** combine that change with review-count increases, ruleset rewrites, or
visibility/ownership changes unless separately approved.
