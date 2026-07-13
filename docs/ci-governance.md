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
| `wheel-smoke` | `harness-ci` |
| `codeql-python` | `codeql` |

The first five contexts were already enforced by legacy branch protection.
After the approved #350 post-merge mutation, CodeQL is required under the
stable `codeql-python` name as the sixth context. `wheel-smoke` (issue #349)
is the seventh declared context: it builds the bridge wheel, installs the
hash-locked runtime set plus the wheel into a clean venv, import/config-smokes
the installed package outside the source tree, and runs `pip check` and
`pip-audit` against the runtime lock. Its live required-check addition follows
the same approved post-merge operation pattern as #350: mutate only
`required_status_checks`, verify the readback, and roll back only the check
list on failure. Workflow job names and this manifest are guarded by
`tests/test_ci_required_contexts.py` so a rename cannot silently strand the
live required context.

## Dependency lock governance (issue #349)

Two hash locks share one generation source, `bridge/pyproject.toml`, and are
regenerated together by `scripts/ccc-deps-lock.sh`:

1. `.github/requirements/bridge-ci.txt` — CI toolchain (ruff, mypy, build,
   pip-audit, pinned pip) plus the bridge dev extra; every CI `pip install`
   uses `--require-hashes` against it.
2. `bridge/requirements.lock.txt` — the runtime set, compiled with the CI lock
   as a pip constraint so runtime nodes install exactly the versions CI
   tested. `bridge/start.sh` installs it with `--require-hashes` by default
   and adds the first-party package with `--no-deps`, so no unhashed
   transitive dependency can enter a node. `CCC_DEPS_UNLOCKED=1` is the
   documented escape hatch for hosts that cannot build a locked artifact.

`tests/test_runtime_deps_lock.py` enforces that the runtime lock stays a
version-consistent subset of the CI lock, that every pin carries hashes, and
that the wheel-smoke/audit gates stay wired. Lock refreshes — including
Dependabot-driven bumps — are regenerated via the script and land as one
verified PR unit validated by the full required-check matrix; lock files are
never hand-edited. The platform marker/lock policy (single Linux-compiled
lock for glibc Linux, macOS, and Termux; sdist hashes cover source builds;
platform-specific deps require explicit environment markers in
`bridge/pyproject.toml`) is documented in `scripts/ccc-deps-lock.sh`.

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
