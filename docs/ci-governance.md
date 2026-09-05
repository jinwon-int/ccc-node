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

`validate-harness` is an aggregator job since #1482: the work runs in
`validate-harness-static` (non-test phases, once) and the
`validate-harness-shard (<n>)` matrix (hook-test suites, bin-packed), and the
`validate-harness` job `needs` both, runs `if: always()`, and exits non-zero
unless every leg reports `success`. The required context therefore keeps its
name and its gate without any branch-protection mutation; the leg jobs are
deliberately NOT declared as required contexts (a matrix leg rename would
strand them, and the aggregator already covers them).

## Dependency lock governance (issue #349)

Two hash locks share one generation source, `bridge/pyproject.toml`, and are
regenerated together by `scripts/ccc-deps-lock.sh`:

1. `.github/requirements/bridge-ci.txt` — CI toolchain (ruff, mypy, build,
   pip-audit, pinned pip) plus the bridge dev extra; every CI `pip install`
   uses `--require-hashes` against it.
2. `bridge/requirements.lock.txt` — the runtime set, compiled with the CI lock
   as a pip constraint so runtime nodes install exactly the versions CI
   tested. `bridge/start.sh` delegates to the standard-library-only
   `bridge/dependency_bootstrap.py`, which installs the lock with
   `--require-hashes` by default and adds the first-party package with
   `--no-deps`, so no unhashed transitive dependency can enter a node.
   `CCC_DEPS_UNLOCKED=1` is the documented escape hatch for hosts that cannot
   build a locked artifact.

`tests/test_runtime_deps_lock.py` enforces that the runtime lock stays a
version-consistent subset of the CI lock, that every pin carries hashes, that
`bridge/requirements.txt` (the `CCC_DEPS_UNLOCKED=1` fallback) mirrors the
runtime pins exactly, and that the wheel-smoke/audit gates stay wired. Lock
refreshes — routine weekly bumps included — are regenerated via the script and
land as one verified PR unit validated by the full required-check matrix; lock
files are never hand-edited. The platform marker/lock policy (single
Linux-compiled lock for glibc Linux, macOS, and Termux; sdist hashes cover
source builds; platform-specific deps require explicit environment markers in
`bridge/pyproject.toml`) is documented in `scripts/ccc-deps-lock.sh`.

## Weekly lock-pair regeneration (issue #1483)

Dependabot **pip version updates are disabled** (`open-pull-requests-limit: 0`
in `.github/dependabot.yml`; the block and its `ignore` entry stay so security
updates keep their configuration). Dependabot cannot run the derivation above
and its `groups` do not span directories, so every pip PR it opened
(#999-#1001, #1453-#1457, #1495) moved a pin in one lock only and failed
`tests/test_runtime_deps_lock.py`, `python-lint` and `wheel-smoke`.
`github-actions` updates are unaffected.

The replacement is the `deps-lock` workflow (`.github/workflows/deps-lock.yml`,
logic in `scripts/ccc-deps-lock-pr.sh`, tested by
`scripts/ccc-deps-lock-pr.test.sh`):

1. runs weekly (Mondays 04:17 UTC) or on demand, checks out `main`, and runs
   `scripts/ccc-deps-lock.sh --upgrade` on CPython 3.11 (or
   `--upgrade-package` per named package when the `upgrade` input is set);
2. if the lock set (`bridge-ci.txt`, `requirements.lock.txt`,
   `bridge/requirements.txt`) is unchanged it exits with "no lock changes";
3. otherwise it commits the set on `deps/lock-pair-<YYYYMMDD>` (bot-owned;
   force-refreshed on a same-day rerun — force-push is confined to that
   prefix), opens or updates the PR against `main` with a per-package pin
   table and `Refs #1483`, and closes any older open `deps/lock-pair-*` PR as
   superseded;
4. dispatches `ci.yml` and `codeql.yml` on the branch. Pushes and PRs made
   with `GITHUB_TOKEN` never trigger `pull_request`/`push` workflows, but
   `workflow_dispatch` does, and check runs attach to the head SHA under the
   same job names — so the required contexts above gate the bot PR without
   any PAT or GitHub App. `ci.yml` therefore declares `workflow_dispatch` and
   must not key any job `if:` on `github.event_name`.

Manual runs:

```sh
gh workflow run deps-lock.yml                                  # every pin may move
gh workflow run deps-lock.yml -f upgrade="ruff==0.14.0 mypy"   # only the named packages
```

Review the bot PR like any other: the pin table is the change list, and the
dispatched required checks are the evidence. If a bot PR falls behind `main`
(strict up-to-date checking), update the branch from the PR page — a
human-initiated update triggers `pull_request` normally — or rerun the
workflow, which regenerates on the current `main`. Repository prerequisite:
Actions setting **"Allow GitHub Actions to create and approve pull requests"**
must be ON, or `gh pr create` fails under `GITHUB_TOKEN`.

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
