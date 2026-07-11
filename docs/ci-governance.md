# CI required-check governance

This document defines the stable check identities for `jinwon-int/ccc-node` and
the boundary between source-controlled workflow policy and live GitHub
protection settings. `.github/required-checks.json` is the reviewable desired
state; GitHub live settings remain the enforcement state.

## Desired checks for `main`

All required checks must come from the GitHub Actions app (`app_id` `15368`)
and strict up-to-date checking remains enabled. `app_id` is the legacy branch
protection API field; rulesets call the analogous field `integration_id`.

| Required context | Workflow |
| --- | --- |
| `validate-harness` | `harness-ci` |
| `python-lint` | `harness-ci` |
| `secret-scan` | `harness-ci` |
| `bridge-tests (3.11)` | `harness-ci` |
| `bridge-tests (3.12)` | `harness-ci` |
| `codeql-python` | `codeql` |

The first five contexts were already enforced by legacy branch protection.
After the approved #350 post-merge mutation, CodeQL is required under the
stable `codeql-python` name as the sixth context. Workflow job names and this
manifest are guarded by `tests/test_ci_required_contexts.py` so a rename cannot
silently strand the live required context.

## CodeQL update atomicity

The CodeQL workflow deliberately has no one-item matrix. GitHub appends matrix
values to check names even when a job has an explicit `name`; a Python-only
matrix would therefore emit `codeql-python (python)` and strand the declared
`codeql-python` required context.

`github/codeql-action/init` and `github/codeql-action/analyze` must use one
identical full commit SHA. Dependabot's `codeql-action-family` group matches
`github/codeql-action/*`, so both action endpoints are updated in one pull
request rather than producing a mixed-version workflow.

## Two protection layers

At the #350 baseline, GitHub exposed two independent protection layers:

1. **legacy branch protection** on `main`: strict required checks, one review,
   dismiss stale reviews, code-owner review, last-push approval, and admin
   enforcement;
2. repository ruleset `18203378`: deletion and non-fast-forward guards plus a
   separate one-review pull-request rule.

GitHub enforces the union. The #350 operation changes only the legacy required
check list. It must not rewrite ruleset `18203378`, change review count, disable
dismiss stale reviews, code-owner review, last-push approval, admin
enforcement, or alter bypass actors. Pull-request conversation resolution
remains disabled; enabling it is a separate governance decision.

## Failure classification

A required check with a started runner and failing test/tool step is a product
or test failure. Fix the source; do not merge.

A run cancelled while jobs have no runner assignment, no `started_at`, and no
executed steps is an **unassigned infrastructure failure**. Preserve the run
URL and job readback, rerun the same SHA once with GitHub's rerun operation,
and do not merge until all required contexts succeed. If the same SHA is again
unassigned, record the provider incident explicitly instead of relabeling it as
a test failure or bypassing protection.

Automatic rerun is intentionally not installed: an Actions-write workflow can
loop or hide a persistent provider failure. The bounded operator action is:

```bash
gh run rerun RUN_ID --repo jinwon-int/ccc-node
```

## Approved post-merge operation

Before mutation, save both of these readbacks as local operator evidence:

```bash
gh api repos/jinwon-int/ccc-node/branches/main/protection
gh api repos/jinwon-int/ccc-node/rulesets/18203378
```

Then update only
`branches/main/protection/required_status_checks`, preserving `strict=true`
and the five existing GitHub Actions checks while adding `codeql-python` with
app ID `15368`. Verify the full branch-protection and ruleset readbacks after
the change.

The no-op boundary is exact set equality with `.github/required-checks.json`.
Do not remove unknown contexts automatically; stop and investigate drift.

## Rollback

If `codeql-python` is not emitted for the merged workflow SHA, rollback only
the required-check list to the pre-change five-context backup. Keep
`strict=true`, all review/admin settings, and ruleset `18203378` unchanged.
The source rename must be reverted in a reviewed PR before retrying the live
addition. Never disable branch protection to unblock a merge.
