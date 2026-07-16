---
name: gh-pr-flow
description: Ship or merge a code change through the PR-first GitHub flow — branch off main, commit, push, review, approve, verify CI and mergeability, then squash-merge with branch cleanup. Use for GitHub PR creation, review, approval, or merge work in jinwon-int repos, including the explicitly approved lane that runs jinon86 GitHub operations on Seoseo without exporting its token. Enforces identity separation, never-push-main-directly, green-CI, and no-admin-bypass rules. Not for Wiki edits (use wiki-record).
---

# gh-pr-flow — PR-first GitHub flow

Use this for any code change that lands in a GitHub repo. Operational repos live under `jinwon-int` where possible. **Never push to `main` directly — branch first.** Local `gh` normally runs as `seoseo-ai`. For Wiki content use the `wiki-record` skill instead.

## Procedure

1. **Sync and branch** (never commit on `main`):
   ```bash
   git checkout main && git pull --ff-only
   git checkout -b <type>/<slug>   # type: feat|fix|docs|chore|spec|merge
   ```

2. **Stage and commit** with a heredoc message + the required trailer:
   ```bash
   git add -A     # or name files explicitly
   git commit -F - <<'EOF'
   <type>(<scope>): <imperative summary>

   <optional body — what & why>

   Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
   EOF
   ```

3. **Push the branch:**
   ```bash
   git push -u origin <branch> 2>&1 | tail -3
   ```

4. **Open the PR** (PR-first; base is normally `main`):
   ```bash
   gh pr create --repo <owner/repo> --base main --head <branch> \
     --title "<type>(<scope>): <summary>" --body "<what / why / evidence>"
   ```
   - Record the PR URL, number, author, and head SHA.

5. **Review before approval.** Read the complete diff, unresolved review threads, requested changes, and repository-specific policy. Do not approve based only on CI.

6. **Select an identity lane.** Never approve a PR with its author's identity.
   - PR authored by `jinon86`: use the local `seoseo-ai` identity when it has access.
   - PR authored by `seoseo-ai` or an allowed bot: when a `jinon86` approval is required and the user explicitly authorizes that identity in the active task, use the Seoseo lane below.
   - Any author allowlist is task-specific. Do not infer that an author is trusted from this skill.

7. **Check mergeability** (do not merge on assumption):
   ```bash
   gh pr view <n> --repo <owner/repo> \
     --json state,author,headRefOid,mergeable,mergeStateStatus,statusCheckRollup,reviews
   ```

8. **Approve and merge when green and mergeable** — squash + cleanup:
   ```bash
   gh pr merge <n> --repo <owner/repo> --squash --delete-branch
   ```
   Do not use `--admin`. Re-read the head SHA immediately before merge.

9. **Verify the terminal state and return to main:**
   ```bash
   gh pr view <n> --repo <owner/repo> --json state,mergedAt,mergeCommit
   git checkout main && git pull --ff-only
   ```

## Seoseo `jinon86` lane

Use `scripts/seoseo-jinon-gh` so the credential remains on Seoseo. The wrapper permits only identity readback and PR view/check/diff/approve/merge operations. It refuses approval by the PR author and pins merges to the freshly read head SHA.

```bash
# Read-only preflight; expected output is exactly jinon86.
claude/skills/gh-pr-flow/scripts/seoseo-jinon-gh identity

# Inspect before mutating.
claude/skills/gh-pr-flow/scripts/seoseo-jinon-gh view <owner/repo> <n>
claude/skills/gh-pr-flow/scripts/seoseo-jinon-gh checks <owner/repo> <n>
claude/skills/gh-pr-flow/scripts/seoseo-jinon-gh diff <owner/repo> <n>

# Use only after explicit user approval in the active task.
claude/skills/gh-pr-flow/scripts/seoseo-jinon-gh approve <owner/repo> <n> --confirm-user-approved
claude/skills/gh-pr-flow/scripts/seoseo-jinon-gh merge <owner/repo> <n> --confirm-user-approved
```

An explicit request such as “서서의 jinon86 권한으로 이 PR을 승인·머지해” satisfies the confirmation for that active task. Merely installing or mentioning this skill does not grant standing approval for unrelated future PRs.

## Rules
- **Never push directly to `main`** — always branch, PR, then merge.
- Verify the active identity before every approval or merge; never approve your own PR.
- Prefer `jinwon-int` for operational repos; treat personal-account duplicates as legacy.
- **Never copy, print, return, or persist the Seoseo token outside Seoseo.** Run the remote `gh` process against its existing node-local authentication; do not use `gh auth token`, read `hosts.yml`, or export `GH_TOKEN`.
- **No raw secrets** in commits, PR bodies, logs, shell traces, or branch names — record only credential locations and handling rules.
- Only merge on **green CI + mergeable**; if blocked, report state (`mergeStateStatus`/failing checks) instead of forcing.
- Stop for unresolved requested changes, review threads, unexpected commits, identity mismatch, or a changed head SHA.
- Fresh approval still applies to releases, tags, admin bypass, credential movement, and other high-risk operations even inside this flow.
