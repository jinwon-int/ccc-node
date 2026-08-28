---
name: gh-pr-flow
description: Ship code through the PR-first GitHub flow on this node, including protected PRs that need an independent cross-account review in either direction — jinon86-authored PR approved via the Seoseo-held seoseo-ai profile (exact-head) or seoseo-ai-authored PR approved/merged as jinon86 (Seoseo-held session) — after fresh explicit user approval. Use when committing or pushing code, opening or merging a PR, resolving REVIEW_REQUIRED, or landing changes in jinwon-int repos. Enforces no direct main pushes, exact-head and green-check validation, independent review, secret-safe credential use, squash merge, and verified cleanup. Not for Wiki edits (use wiki-record).
---

# gh-pr-flow — PR-first GitHub flow

Use this for code changes that land in GitHub. Operational repos live under
`jinwon-int` where possible. Never push directly to `main`; use a branch and PR.
For Wiki content use `wiki-record` instead.

## Identities and review directions

A node's local `gh` holds `jinon86` (typically the only local account).
`seoseo-ai` review authority lives on Seoseo as an isolated profile config
(`/root/.config/gh-seoseo-ai`), and Seoseo separately holds an authorized
`jinon86` session. Check with `gh auth status`. Whichever account authored
the PR, the OTHER account reviews it — both directions are supported
symmetrically:

| PR author | Reviewer / merger | Mechanism |
|---|---|---|
| `jinon86` | `seoseo-ai` reviews; `jinon86` merges | `approve-via-seoseo.sh --review-profile seoseo-ai` (Seoseo-held profile, exact-head) |
| `seoseo-ai` | `jinon86` reviews and, if needed, merges | `approve-via-seoseo.sh --review-profile jinon86` (exact-head) / `merge-via-seoseo.sh` (Seoseo-held session over SSH) |

Every cross-account review or merge is a privileged credential use and needs
fresh explicit user approval in the current conversation, in both directions.

## GitHub transport policy

- Use local `git` and the authenticated `gh` CLI for every GitHub read and write.
- Do not use GitHub App, connector, MCP, or plugin tools unless the user
  explicitly requests that transport in the current task.
- If `gh` fails, report the error; do not automatically retry through a GitHub
  connector.
- Run `gh auth status` before the first authenticated GitHub operation when the
  current session has not already verified it.

## Procedure

1. Sync and branch:

   ```bash
   git switch main
   git pull --ff-only
   git switch -c <type>/<slug>
   ```

2. Stage only the intended files and commit with the required trailer:

   ```bash
   git add <file>...
   git commit -F - <<'EOF'
   <type>(<scope>): <imperative summary>

   <optional body — what and why>

   Co-Authored-By: <the model running this session> <noreply@anthropic.com>
   EOF
   ```

3. Push and open a PR against the repository's default branch. It is `main` in
   most `jinwon-int` repos but **`master` in `seoyoon-family-wiki`** — the review
   and merge helpers verify the base against `gh api repos/<owner>/<repo> --jq
   .default_branch`, so a PR opened against the wrong branch is refused:

   ```bash
   git push -u origin <branch>
   base="$(gh api repos/<owner>/<repo> --jq .default_branch)"
   gh pr create --repo <owner/repo> --base "$base" --head <branch> \
     --title "<type>(<scope>): <summary>" --body "<what / why / evidence>"
   ```

4. Inspect identity, review, exact head, checks, and merge state:

   ```bash
   gh pr view <n> --repo <owner/repo> \
     --json author,reviewRequests,reviewDecision,headRefOid,isDraft,mergeable,mergeStateStatus,statusCheckRollup
   ```

5. Resolve `REVIEW_REQUIRED` with an independent reviewer. Never use `--admin`
   merely to bypass branch protection.

   - Old approvals, memory, environment state, or approval for another PR do
     not count. If approval is absent or ambiguous, stop and ask.
   - **One narrow exception, and it is not this one.** Some repos deny
     infrastructure paths (`scripts/`, `.github/`) at a required check on every
     branch and deliberately leave `enforce_admins` false, documenting the admin
     merge as the *intended* way in. That is a sanctioned hatch, not a bypass —
     see `infra-gate-admin-merge-discipline`, which also covers the trap that
     such gates exit before running their own tests. It applies only when the
     gate's own source sanctions it; absent that sentence, this rule stands.
   - After approval, set the approval flag only on the one approved command.

   Helper scripts live next to this SKILL.md. Resolve the directory once —
   the installed copy first, the template checkout as fallback (e.g. when an
   installed copy is stale, as on gongmyoung 2026-08-07):

   ```bash
   GH_PR_FLOW_DIR="${CCC_CLAUDE_DIR:-$HOME/.claude}/skills/gh-pr-flow"
   if [ ! -d "$GH_PR_FLOW_DIR" ]; then
     for _cand in /opt/ccc-node "$HOME/ccc-node" /root/ccc-node; do
       [ -d "$_cand/claude/skills/gh-pr-flow" ] || continue
       GH_PR_FLOW_DIR="$_cand/claude/skills/gh-pr-flow"; break
     done
   fi
   ```

   (The fallback tries the fleet's checkout locations in order — `/opt/ccc-node`,
   `$HOME/ccc-node`, `/root/ccc-node` — instead of hardcoding one; nodes install
   the repo in different places, which previously forked this snippet per node.)

   **Direction A — `jinon86`-authored PR, `seoseo-ai` reviews (Seoseo-held
   profile):**

   Both directions use the same exact-head profile helper. It ships with the
   managed Codex skills that `setup.sh` installs on every node. Resolve it
   first, with template-checkout fallback:

   ```bash
   SEOSEO_FLOW_DIR="${CODEX_HOME:-$HOME/.codex}/skills/gh-pr-flow/scripts"
   if [ ! -d "$SEOSEO_FLOW_DIR" ]; then
     for _cand in /opt/ccc-node "$HOME/ccc-node" /root/ccc-node; do
       [ -d "$_cand/codex/skills/gh-pr-flow/scripts" ] || continue
       SEOSEO_FLOW_DIR="$_cand/codex/skills/gh-pr-flow/scripts"; break
     done
   fi
   ```

   ```bash
   head_sha="$(gh pr view <n> --repo <owner/repo> --json headRefOid --jq .headRefOid)"
   CCC_EXPLICIT_USER_APPROVAL=1 \
     bash "$SEOSEO_FLOW_DIR/approve-via-seoseo.sh" \
     --review-profile seoseo-ai --repo <owner/repo> --pr <n> \
     --expected-head "$head_sha" --operator-approved
   ```

   The helper runs `gh` on Seoseo with the isolated `seoseo-ai` profile config
   — credentials never leave Seoseo and are never printed, copied, or switched.
   It fail-closes unless the remote actor is `seoseo-ai`, the open default-
   branch PR is authored by `jinon86`, `seoseo-ai` is the requested reviewer
   (request it with `gh pr edit --add-reviewer seoseo-ai` when missing), the
   head matches `--expected-head` exactly, the PR is mergeable, and every
   reported check on that head is successful. `--operator-approved` records
   the fresh user authorization for this exact PR and head. After approval,
   `jinon86` (the local account) merges normally in step 6.

   **Direction B — `seoseo-ai`-authored PR, `jinon86` reviews (via Seoseo):**

   Same helper, opposite profile:

   ```bash
   head_sha="$(gh pr view <n> --repo <owner/repo> --json headRefOid --jq .headRefOid)"
   CCC_EXPLICIT_USER_APPROVAL=1 \
     bash "$SEOSEO_FLOW_DIR/approve-via-seoseo.sh" \
     --review-profile jinon86 --repo <owner/repo> --pr <n> \
     --expected-head "$head_sha" --operator-approved
   ```

   It runs `gh` on Seoseo with the root-owned `jinon86` config and fail-closes
   unless the remote actor is `jinon86`, the open default-branch PR is authored
   by `seoseo-ai` with `jinon86` requested, the head matches `--expected-head`
   exactly, the PR is mergeable, and every reported check on that head is
   successful. The approval is commit-bound and the credential never leaves
   Seoseo.

6. With required review and checks green, squash-merge normally:

   ```bash
   gh pr merge <n> --repo <owner/repo> --squash --delete-branch
   ```

   If the local identity lacks repository merge permission, use the exact-head
   Seoseo merge fallback below. Do not weaken branch protection.

   **`mergeable: UNKNOWN` is "not computed yet", not "blocked".** GitHub builds
   a throwaway test merge commit in a background job; until it finishes the
   GraphQL enum reads `UNKNOWN` ("The mergeability of the pull request is still
   being calculated") and REST `mergeable` reads `null`, which the REST docs
   tell you to resolve by resubmitting the request. Closed and merged PRs also
   report `UNKNOWN`, so it is never by itself evidence of a problem. Re-read
   until it settles instead of reaching for a workaround:

   ```bash
   for i in $(seq 1 30); do
     read -r m s <<<"$(gh pr view <n> --repo <owner/repo> \
       --json mergeable,mergeStateStatus --jq '"\(.mergeable) \(.mergeStateStatus)"')"
     [ "$m" = UNKNOWN ] || { echo "mergeable=$m mergeStateStatus=$s"; break; }
     sleep 10
   done
   ```

   Then act on `mergeStateStatus`, not on the merge command's failure text:
   `CLEAN` merge; `UNSTABLE`/`BLOCKED` means checks or required reviews are the
   real gate — fix those; `DIRTY` is a genuine conflict (see
   `github-merge-state-conflict-diagnosis`); `BEHIND` means the base moved.

   Two failures here look like merge problems but are repository settings, and
   neither justifies `--admin`, weaker protection, or a credential escalation:

   `gh pr view --json` exposes neither capability flag — it only carries
   `autoMergeRequest`. Read them over GraphQL before attempting either command:

   ```bash
   gh api graphql -f query='
   query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){pullRequest(number:$n){
     viewerCanEnableAutoMerge viewerCanUpdateBranch}}}' \
     -F o=<owner> -F r=<repo> -F n=<n> --jq .data.repository.pullRequest
   ```

   - `gh pr merge --auto` fails when the repo has `allow_auto_merge: false`
     (`viewerCanEnableAutoMerge: false`; also `gh api repos/<owner>/<repo> --jq
     .allow_auto_merge`). The observed message is roughly `Auto merge is not
     allowed for this repository` — match it loosely, since GitHub does not
     document the string and a configured merge queue can produce it too.
     Enabling the setting is a repository-settings change and a separate
     approval; without it, poll and merge in the foreground instead.
   - `gh pr update-branch` only does something when the head is genuinely
     behind. Gate it on `viewerCanUpdateBranch`, which GitHub documents as
     `false` when the head is already up to date. The REST endpoint answers
     `202 Accepted` with `"Updating pull request branch."` — an async
     acknowledgement, not proof a commit was created — so confirm by comparing
     `headRefOid` before and after rather than trusting the success line.

7. Verify and clean up:

   ```bash
   gh pr view <n> --repo <owner/repo> --json state,mergedAt,mergeCommit
   git switch main
   git pull --ff-only
   git ls-remote --exit-code --heads origin <branch>
   ```

   A nonzero `ls-remote` result means the remote branch is absent, as expected.
   `git branch -d <branch>` may reject a squash-merged branch because squash
   does not preserve ancestry. Use `git branch -D <branch>` only after verifying
   the PR is merged, `main` contains the change, and the remote branch is gone.
   If the PR links an issue with a closing keyword, verify that issue is closed.

## Seoseo `jinon86` exact-head merge fallback

Use this only when all of the following are true:

- The operator explicitly approved merging this specific repository and PR in
  the current task. Approval does not carry to another PR or a changed head.
- The local identity cannot merge it, while Seoseo already has the authorized
  `jinon86` GitHub session.
- The PR is non-draft, targets `main`, is `MERGEABLE`/`CLEAN`, has all required
  reviews, and its exact head has passing GitHub checks or documented equivalent
  validation.

Capture the exact head locally, then call the fail-closed helper:

```bash
head_sha="$(gh pr view <n> --repo <owner/repo> --json headRefOid --jq .headRefOid)"
bash "$GH_PR_FLOW_DIR/merge-via-seoseo.sh" \
  --repo <owner/repo> --pr <n> --expected-head "$head_sha" \
  --operator-approved
```

The helper verifies actor `jinon86`, re-reads the PR on Seoseo, requires the
same head SHA and clean merge state, rejects pending or failed checks, and uses
GitHub's merge API with the SHA precondition. It never uses an admin bypass. A
PR with no reported checks is allowed only when exact-head equivalent evidence
is already recorded and the operator is told that GitHub reported no checks.

Delete the contributor branch using the identity that owns it. If cleanup
permission is unavailable, report it rather than moving a credential.

## Security and merge rules

- Never push directly to `main`; always use a branch and PR.
- The PR author cannot approve their own PR. Keep author and reviewer identities
  independent.
- Only merge with green required checks and a mergeable state. Report failed or
  pending checks instead of forcing the merge.
- Never read, print, copy, persist, export, or re-login with the Seoseo token.
  Run `gh` on Seoseo so the credential stays there. Do not enable shell trace,
  switch the persistent local account, or put credentials in arguments,
  commits, PR text, logs, or memory.
- The Seoseo-held `seoseo-ai` profile credential follows the same discipline:
  use it only through the exact-head `approve-via-seoseo.sh` profile helper,
  which runs `gh` on Seoseo with the isolated profile config for a single
  approved invocation. Never print it, never copy it off Seoseo, never
  `gh auth switch` to it, and never reuse one approval for a second invocation
  or a different head.
- An approval flag records a fresh user decision; it does not grant standing
  authority. Set it for one approved invocation only. Review approval and merge
  approval are separate privileged writes unless the user's current instruction
  explicitly authorizes both for that exact PR.
- A merge instruction authorizes the merge, not token disclosure, credential
  transfer, release/publish, deploy, restart, migration, or another mutation.
