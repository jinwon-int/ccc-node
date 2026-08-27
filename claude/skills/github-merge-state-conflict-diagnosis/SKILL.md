---
name: github-merge-state-conflict-diagnosis
description: Diagnose a PR whose merge/CI state is untrustworthy — no checks appear at all (statusCheckRollup empty), mergeStateStatus is DIRTY/BEHIND/BLOCKED, or mergeable stays UNKNOWN — by reading the right field, testing conflicts locally without touching the worktree, and detecting a stale base whose green CI no longer proves anything. Use when CI is silent on a PR, when a gh-ci-wait poll times out with nothing queued, when mergeable will not resolve, or before trusting a green check on a PR whose base has moved.
---

# GitHub merge state & conflict diagnosis

Three distinct failures wear the same costume — "the PR looks stuck":

| Symptom | Field to read | Real cause |
|---|---|---|
| No checks at all | `statusCheckRollup == []` + `mergeStateStatus == DIRTY` | Conflict — GitHub cannot build the merge ref, so CI never runs |
| Merge state won't settle | `mergeable == UNKNOWN` | GitHub is still computing it (async), or the computation is wedged |
| Green, but meaningless | `mergeStateStatus == BEHIND` | **Stale base** — CI passed against an old base commit |

`mergeable` (MERGEABLE/CONFLICTING/UNKNOWN) and `mergeStateStatus`
(CLEAN/DIRTY/BEHIND/BLOCKED/UNSTABLE/HAS_HOOKS) answer *different* questions.
Read both; never infer one from the other.

## When to Use

- You open or await a PR and CI checks never appear (no "checks pending" state).
- A `gh-ci-wait` registration was created but the poll timed out with nothing queued.
- `mergeable` stays UNKNOWN and merge safety must still be determined.
- The base branch advanced and you must decide whether the existing green CI still counts.
- A PR shows mergeable in the UI but CI artifacts are missing.

## Procedure

### 1. Read the full merge state in one query

```bash
gh pr view <n> --repo <owner>/<repo> \
  --json mergeable,mergeStateStatus,statusCheckRollup,baseRefName,headRefOid,commits
```

**`gh pr view` has no `baseRefOid` field** — it exposes `baseRefName` (the branch
name) but not the base *commit* the PR is diffed against. Verified 2026-08-28:
passing `baseRefOid` fails with `Unknown JSON field`. For the base SHA you must
drop to the REST API:

```bash
gh api repos/<owner>/<repo>/pulls/<n> -q '.base.sha, .mergeable, .mergeable_state'
```

Note the REST field is `mergeable_state` (snake_case, lowercase values like
`dirty`/`blocked`/`clean`) while the GraphQL/`gh pr view` field is
`mergeStateStatus` (SCREAMING_CASE). They are the same signal in two spellings;
do not mix them in one script.

### 2. Classify

- **DIRTY + rollup `[]`** → conflict is blocking CI. Go to §4 (rebase).
- **CLEAN + rollup `[]`** → CI not configured or the workflow filtered this
  event out. Inspect `.github/workflows/` triggers (`on.pull_request.paths`,
  `branches`, `types`) and whether the workflow is disabled. Note that a
  required-but-never-run check is an enforcement issue — see
  `github-required-check-enforcement-audit`.
- **BEHIND** → stale base. Go to §5.
- **UNKNOWN** → go to §3.
- **rollup populated** → CI is live; `gh pr checks <n> --repo <owner>/<repo>`.

### 3. mergeable = UNKNOWN — re-poll first, then verify locally

UNKNOWN is usually **transient**: GitHub computes mergeability lazily and the
first query often triggers it. Poll a few times before escalating.

```bash
for i in 1 2 3 4 5; do
  gh pr view <n> --repo <owner>/<repo> --json mergeable -q .mergeable
  sleep 5
done
```

If it will not resolve, determine the truth locally — **without touching your
worktree or index**:

```bash
git fetch origin <base> <pr-branch>
git merge-tree --write-tree origin/<base> origin/<pr-branch> >/dev/null
echo "exit=$?"   # 0 = merges cleanly, 1 = conflicts, >1 = error
```

`git merge-tree --write-tree` (git ≥ 2.38) is the right tool: it computes the
merge in the object database and reports conflicts without a checkout, so there
is nothing to abort and no dirty state to clean up.

If you must use the older form instead, pair it correctly:

```bash
git merge --no-commit --no-ff origin/<base>; git merge --abort
```

Treat the local result as ground truth when the API says UNKNOWN — but record
it as *local evidence*, not as a GitHub state.

### 4. Conflict (DIRTY) — rebase to unblock CI

```bash
git fetch origin && git rebase origin/<base>
git push --force-with-lease
```

Then wait 30–60s and re-query §1 to confirm `statusCheckRollup` is populated.
If checks are still absent after a clean rebase, the workflow is likely
disabled or filtered — inspect `.github/workflows/` or ask the maintainer.

### 5. Stale base (BEHIND) — the green check is not evidence

**This is the quiet one.** A PR can be conflict-free, mergeable, and fully
green, while its CI ran against a base commit from days ago. Merging it lands
code that was never tested against current `main`.

```bash
git fetch origin
gh api repos/<owner>/<repo>/pulls/<n> -q .base.sha                      # what CI tested against
git rev-parse origin/<base>                                             # what you'd actually merge into
git log --oneline <base-sha>..origin/<base> | head                      # what moved underneath
```

Decide by what moved, not by the check color:

- Base moved and any of `.github/workflows/`, gate scripts, lockfiles, or
  shared modules changed → **rebase and re-run CI**. Non-negotiable.
- Base moved in unrelated paths only → rebasing is still the safe default;
  merging on stale-green is a judgement call to state explicitly, not a silent one.

After rebase, wait 30–60s, confirm CI re-ran **on the new head SHA** (not the
cached old run), and re-check merge state.

### 6. Confirm the merge actually landed

```bash
gh pr view <n> --repo <owner>/<repo> --json state,mergedAt,mergeCommit -q '{state,mergedAt,oid:.mergeCommit.oid}'
git fetch origin && git log --oneline -1 <mergeCommit-oid>
```

**Do not** verify with `git log <base> | grep <pr-head-sha>`. Squash and rebase
merges create *new* SHAs — the PR head commit will not appear, and its absence
proves nothing. `mergeCommit.oid` is the only reliable link.

## Safety

- Never force-push to a protected branch without explicit approval.
- Rebasing rewrites SHAs — confirm no exact-head review registration, dependent
  branch, or pinned CI reference depends on the old head.
- Always `--force-with-lease`, never bare `--force`.
- Prefer `merge-tree` over a real merge test; if you do run a merge test, abort
  it in the same command so a failure cannot strand a half-merged worktree.
- A stale base is a *reason to re-run CI*, never a reason to bypass a check.

## Verification

- After rebase: `statusCheckRollup` non-empty, and at least one check queued within 60s.
- The check run you trust is attached to the **current** head SHA.
- For stale base: the REST `.base.sha` matches `git rev-parse origin/<base>` before merging.
- For UNKNOWN: the local `merge-tree` exit code is recorded alongside the final API state.
- Merge confirmed via `mergeCommit.oid`, not via a head-SHA grep.
- Multi-session work: state written to `~/.claude/state/working-state.md` so the next session knows CI was unblocked.
