---
name: github-merge-state-conflict-diagnosis
description: Diagnose missing PR checks, conflicting or stale branches, and unresolved GitHub mergeability. Compare CLI and API fields, test the exact PR objects without changing the user's checkout, and verify which revision CI actually tested. Use when checks never appear, mergeable remains UNKNOWN, or the base advances beneath a reviewed PR.
---

# Diagnose PR merge and CI state

`mergeable` answers whether GitHub can compute a clean merge. `mergeStateStatus`
also reflects checks, reviews and base freshness. Neither replaces the actual
required-check and review evidence.

## Read the current state

```bash
gh pr view <n> --repo <owner>/<repo> \
  --json state,isDraft,mergeable,mergeStateStatus,statusCheckRollup,baseRefName,headRefOid
```

The CLI field list depends on the installed `gh` version. Check its help and
read-only JSON output before using `baseRefOid`; recent builds expose it, while
older builds may reject it. Do not turn an older version's missing-field response
into a universal API claim. A REST read is another way to inspect the current
base reference:

```bash
gh api repos/<owner>/<repo>/pulls/<n> \
  -q '{base:.base.sha,head:.head.sha,mergeable,mergeable_state}'
```

| Meaning | REST | CLI/GraphQL |
| --- | --- | --- |
| Mergeability | `mergeable`: boolean or null | `mergeable`: MERGEABLE, CONFLICTING or UNKNOWN |
| Merge/protection state | `mergeable_state`, e.g. `dirty` | `mergeStateStatus`, e.g. DIRTY |

Treat null/UNKNOWN as not yet determined. Do not compare a REST boolean with a
CLI enum string. Preserve the original surface and time in diagnostic records.

The current PR `base.sha`/`baseRefOid` describes current PR metadata. It is **not**
a record of the base used by a previous CI run; inspect that run's event,
checkout and artifacts to establish what was tested.

## Classify before changing source

- **DIRTY/CONFLICTING with no checks:** a conflict can prevent creation of a PR
  merge ref and its CI event. Confirm the conflict and inspect workflow triggers;
  the empty rollup alone does not establish the cause.
- **CLEAN with no checks:** inspect enabled workflows and their event/path/base
  filters, then the effective required-check set. Missing required checks are
  unresolved, not passing. If a specialized enforcement-audit skill is absent,
  these direct source/ruleset reads remain necessary.
- **BEHIND:** inspect the changed base and the exact revision/event tested by CI.
  A merge queue may test a new combined revision; do not infer that an old PR
  check already tested it.
- **BLOCKED/UNSTABLE:** read the specific failed or missing review/check gate.
- **UNKNOWN:** poll briefly, then use local diagnostics below if necessary.

`gh pr checks <n> --repo <owner>/<repo>` displays current checks. A check on an
older head or unrelated event cannot satisfy a requirement for the current head.
A permanently missing event is not repaired by repeatedly polling it.

## Diagnose UNKNOWN without changing the checkout

GitHub computes mergeability asynchronously. Re-read a few times with bounded
waits. If it remains UNKNOWN, establish a **local** conflict result using exact
objects; this does not authorize merging or replace server-side gates.

First confirm `origin` points to the intended base repository. Record the PR's
full head SHA as `EXACT_HEAD`, then fetch the base and GitHub PR ref. The PR ref
works for a fork whose branch name does not exist in the base repository:

```bash
git fetch --no-tags origin "refs/heads/<base>" || exit 1
BASE_HEAD=$(git rev-parse FETCH_HEAD) || exit 1
git fetch --no-tags origin "refs/pull/<n>/head" || exit 1
FETCHED_HEAD=$(git rev-parse FETCH_HEAD) || exit 1
test "$FETCHED_HEAD" = "$EXACT_HEAD" || exit 1
git merge-tree --write-tree "$BASE_HEAD" "$EXACT_HEAD"
```

Run the example in an isolated shell so its failure does not exit an interactive
session. Stop on either fetch failure; do not reuse a stale FETCH_HEAD. For an
executable script, make each fetch/resolve step explicitly fail closed before
continuing. Re-read the remote PR head after the test; head drift invalidates the
result for the new revision. Fetching changes the object database/FETCH_HEAD,
but no command above checks out a branch or mutates the index or working files.

Capture both stdout and stderr plus the exit code in a protected diagnostic
location outside the user's checkout. `merge-tree --write-tree` reports a clean
result tree on success, conflict details on a conflict, and diagnostics for
invalid objects or usage errors. Do not discard its diagnostics and label every
nonzero exit a conflict. Check `git merge-tree -h` on the installed version and
verify clean/conflicting/invalid-object cases in temporary repositories. The
older three-argument merge-tree form has different semantics and its exit status
alone is not equivalent evidence.

If the required merge-tree interface is unavailable, use an isolated compatible
tool environment or report local diagnostics unavailable. Never substitute a
real merge followed by abort in the user's checkout. Record the exact base/head,
local result and final GitHub state separately.

## Resolve an actual conflict or stale base

Source changes require a separate step in an isolated PR worktree. First check
whether main already fixes or supersedes the change; avoid reintroducing an old
implementation just to make textual conflicts disappear. Review the semantic
resolution and run appropriate regression tests.

When rebasing an owned PR branch, preserve the original head for recovery,
confirm no concurrent changes or dependent work are overwritten, and push with
an exact lease. Never rewrite a protected base or use a bare force push. A merge
from the base into the PR is another option when repository policy permits it.
Both approaches change the review target and require new CI/review evidence.

Assess stale CI by the **run's actual tested objects**, not only by current PR
metadata. Record the workflow/run ID, event, head and, for merge-ref or merge-group
runs, the tested combined commit/base from checkout logs or artifacts. If that
binding cannot be established, report it unknown. Re-run on the refreshed target
or let the required merge queue validate its combined revision.

After a refresh, verify the expected event actually starts and completes on the
new target. No check appearing within an arbitrary number of seconds proves
neither success nor failure by itself; investigate event filters and run state.
Follow `gh-pr-flow` for independent exact-head approval and normal merging.

## Verify the merge

```bash
gh pr view <n> --repo <owner>/<repo> \
  --json state,mergedAt,mergeCommit,headRefOid
```

Require MERGED plus a merge timestamp and commit, tied to the reviewed head and
submission evidence. Fetch the intended base and verify the reported merge
commit is its ancestor (`git merge-base --is-ancestor <merge-oid> origin/<base>`).
Merely printing `git log -1 <merge-oid>` proves object existence, not that it
landed on the base. Squash/rebase merges produce new commit IDs, so searching the
base log for the old PR head is not a valid completion check. Queue admission or
auto-merge enablement is not completion either.

## Continuity and safety

Keep diagnostic and recovery records free of secrets or private response bodies.
A working-state checkpoint records what was observed, the exact target and the
next authorized step. It does not prove that CI was unblocked or a merge landed.
Use `gh-ci-wait` when promising a durable continuation after CI, and record its
actual wait ID rather than a prose promise. Do not weaken protection or treat a
local clean merge result as permission to bypass a remote gate.
