# Branch protection and CODEOWNERS policy

`main` is protected by both legacy GitHub branch protection and a repository
ruleset. This repository intentionally keeps source-controlled ownership and CI
desired-state files separate from live protection settings so changes stay
auditable and reversible. Required-check identity, dual-layer protection, and
infra-failure handling are defined in [`ci-governance.md`](ci-governance.md).

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

## 2026-07-18 — required-review requirement removed (operator decision)

With explicit operator approval (single-operator fleet; review latency was the
sole merge bottleneck across the 2026-07-17/18 hardening series), the live
`main` protection was changed:

- action: `DELETE /branches/main/protection/required_pull_request_reviews` —
  PR review approval is no longer required to merge.
- preserved (no-op boundary): all 6 required status checks, strict up-to-date
  branches, and admin enforcement — merges still require full CI green on the
  exact head, admins included.
- consequence: independent review (seoseo-ai) remains AVAILABLE and welcome —
  reviewers can still comment/approve/request changes — but a standing
  `CHANGES_REQUESTED` no longer hard-blocks a merge. The PR-first flow itself
  is unchanged: no direct pushes to `main`.
- rollback: re-create `required_pull_request_reviews` with
  `required_approving_review_count=1`, stale-review dismissal, and code-owner
  reviews, per the section above. Do not raise the count above `1` while only
  two write-capable maintainers exist.
