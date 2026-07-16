---
name: gh-pr-flow
description: Ship a code change through the PR-first GitHub flow on this node — branch off main, commit with a Co-Authored-By trailer, push, open a PR, verify the exact head and checks, and squash-merge with branch cleanup. Use whenever you are about to commit/push code, open or merge a pull request, or land a change in jinwon-int repos (ccc-node, a2a-nexus, wiki-agent, etc.). Includes the approved Seoseo-hosted jinon86 merge fallback when the local seoseo-ai identity lacks organization merge permission. Not for Wiki edits (use wiki-record).
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

   If GitHub rejects the merge because local `seoseo-ai` lacks repository
   permission, use the Seoseo fallback below. Do not use `--admin` or weaken
   branch protection.

7. **Return to main:**
   ```bash
   git checkout main && git pull --ff-only
   ```

## Seoseo `jinon86` merge fallback

Use this only when all of the following are true:

- The operator explicitly approved merging this specific repository and PR in
  the current task. Approval does not carry to another PR or a changed head.
- The local identity cannot merge it, while Seoseo already has the authorized
  `jinon86` GitHub session.
- The PR is non-draft, targets `main`, is `MERGEABLE`/`CLEAN`, and its exact
  head has passing GitHub checks or documented equivalent validation.

Never read, print, copy, export, or re-login with the Seoseo token. Execute
`gh` on Seoseo over SSH so the credential stays there. First capture the exact
head locally:

```bash
head_sha="$(gh pr view <n> --repo <owner/repo> --json headRefOid --jq .headRefOid)"
```

Then run the bundled fail-closed helper:

```bash
bash ~/.claude/skills/gh-pr-flow/merge-via-seoseo.sh \
  --repo <owner/repo> --pr <n> --expected-head "$head_sha" \
  --operator-approved
```

The helper verifies the remote GitHub actor is `jinon86`, re-reads the PR on
Seoseo, requires the same head SHA and clean merge state, rejects pending or
failed checks, and uses GitHub's merge API with the SHA precondition. It never
uses an admin bypass. A PR with no reported checks is allowed only when the
agent has already recorded exact-head equivalent test/build evidence and tells
the operator that the target repository reported no checks.

After merge, delete the contributor branch using the identity that owns that
branch. If cleanup permission is unavailable, report it rather than moving a
credential.

## Rules
- **Never push directly to `main`** — always branch, PR, then merge.
- `gh` identity is `seoseo-ai`; `jinon86`-authored PRs get a `seoseo-ai` reviewer/merger when applicable.
- Prefer `jinwon-int` for operational repos; treat personal-account duplicates as legacy.
- **No raw secrets** in commits, PR bodies, or branch names — reference locations/handling only (read keys from `~/.hermes/.env`).
- Only merge on **green CI + mergeable**; if blocked, report state (`mergeStateStatus`/failing checks) instead of forcing.
- Treat each Seoseo-backed merge as a privileged write: fresh, PR-specific
  operator approval is mandatory even though the credential already exists.
- A merge request authorizes the merge operation, not token disclosure,
  credential transfer, release/publish, deploy, restart, or other mutations.
- Fresh approval still required for releases/secrets/migrations even inside this flow.
