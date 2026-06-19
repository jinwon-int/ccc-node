---
name: gh-pr-flow
description: Ship a code change through the PR-first GitHub flow on this node — branch off main, commit with a Co-Authored-By trailer, push, open a PR with gh (identity seoseo-ai), check mergeability, and when green squash-merge with branch cleanup. Use whenever you are about to commit/push code, open or merge a pull request, or land a change in jinwon-int repos (ccc-node, a2a-nexus, wiki-agent, etc.). Enforces the never-push-main-directly rule and the jinon86 -> seoseo-ai review convention. Not for Wiki edits (use wiki-record).
---

# gh-pr-flow — PR-first GitHub flow

Use this for any code change that lands in a GitHub repo. Operational repos live under `jinwon-int` where possible. **Never push to `main` directly — branch first.** `gh` runs as `seoseo-ai`. For Wiki content use the `wiki-record` skill instead.

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

4. **Open the PR** (PR-first; base is always `main`):
   ```bash
   gh pr create --repo <owner/repo> --base main --head <branch> \
     --title "<type>(<scope>): <summary>" --body "<what / why / evidence>"
   ```
   - PRs authored by `jinon86` need a `seoseo-ai` reviewer/merger when applicable — request it.

5. **Check mergeability** (don't merge on assumption):
   ```bash
   gh pr view <n> --repo <owner/repo> \
     --json state,mergeable,mergeStateStatus,statusCheckRollup
   ```

6. **Merge when green & mergeable** — squash + cleanup:
   ```bash
   gh pr merge <n> --repo <owner/repo> --squash --delete-branch
   ```

7. **Return to main:**
   ```bash
   git checkout main && git pull --ff-only
   ```

## Rules
- **Never push directly to `main`** — always branch, PR, then merge.
- `gh` identity is `seoseo-ai`; `jinon86`-authored PRs get a `seoseo-ai` reviewer/merger when applicable.
- Prefer `jinwon-int` for operational repos; treat personal-account duplicates as legacy.
- **No raw secrets** in commits, PR bodies, or branch names — reference locations/handling only (read keys from `~/.hermes/.env`).
- Only merge on **green CI + mergeable**; if blocked, report state (`mergeStateStatus`/failing checks) instead of forcing.
- Fresh approval still required for releases/secrets/migrations even inside this flow.
