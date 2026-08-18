---
name: github-merge-state-conflict-diagnosis
description: Detect why a PR's CI checks don't appear using mergeStateStatus and statusCheckRollup — rapid diagnosis of merge conflicts silently blocking CI. Use when a PR shows no checks at all ("CI is silent"), a gh-ci-wait poll times out with nothing queued, or statusCheckRollup is empty despite CI being configured.
---

# GitHub Merge State Conflict Diagnosis

When CI checks fail to appear on a PR (statusCheckRollup is empty despite CI configuration), the PR likely has a merge conflict that prevents GitHub from constructing the merge ref. This skill detects that state via the GitHub CLI and guides resolution.

## When to Use

- You open or await a PR and CI checks never appear (no "checks pending" state)
- A PR shows as mergeable in the UI but CI artifacts are missing
- A `gh-ci-wait` registration was created but the poll timeout fires without any checks queuing
- You need to triage "why is CI silent on this PR?"

## Procedure

1. Query the PR's merge state:
   ```bash
   gh pr view <number> --repo <owner>/<repo> --json mergeStateStatus,statusCheckRollup,commits
   ```

2. Interpret the result:
   - **mergeStateStatus = DIRTY + statusCheckRollup = []** → merge conflict blocking CI; rebase to unblock
   - mergeStateStatus = CLEAN + statusCheckRollup = [] → CI not configured or workflow filtered out; check `.github/workflows/` triggers and branch protection rules
   - statusCheckRollup populated → CI is running; use `gh pr checks <number> --repo <owner>/<repo>` for details

3. If DIRTY + empty, rebase the branch:
   ```bash
   git fetch origin && git rebase origin/main
   git push --force-with-lease
   ```
   Then re-query to confirm statusCheckRollup is now populated.

4. Wait 30–60 seconds for GitHub to queue the first check.

5. If checks still absent after successful rebase, the CI workflow may be disabled; inspect `.github/workflows/` or ask the repo maintainer.

## Safety

- Never force-push to a protected branch without explicit approval.
- Rebasing changes commit SHAs; confirm no other branches or registered exact-head reviews depend on the old SHAs.
- Use `--force-with-lease` to avoid overwriting concurrent upstream changes.

## Verification

- After rebase, re-run the merge state query; confirm statusCheckRollup is no longer empty.
- Observe at least one check queued within 60 seconds.
- If this is a multi-session task, document the state in `~/.claude/state/working-state.md` so the next session knows CI was unblocked.
