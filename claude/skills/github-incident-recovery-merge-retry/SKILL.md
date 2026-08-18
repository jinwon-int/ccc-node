---
name: github-incident-recovery-merge-retry
description: Recover a green, mergeable PR through a GitHub infrastructure outage — poll the githubstatus components API until Actions/Pull Requests recover, re-run only failed workflow jobs, then land the PR with an exact-head (SHA-pinned) squash merge that falls back from GraphQL to REST and retries on a fixed interval via a detached background watcher. Use when GitHub returns 5xx/timeouts on CI or merge writes while the PR itself has no code problem. Established during the 2026-08-17 GitHub incident (jinwon-int/nclex #210 campaign).
---

## When to Use
- GitHub Actions or PR/API components are degraded (5xx, timeouts) and workflow runs failed for infra reasons, not code
- A merge attempt returns 503/transient errors while the PR is green, mergeable, and conflict-free
- You need to land the PR without manual escalation, possibly across session boundaries

## Preconditions (validate BEFORE registering any watcher)
Failure must be **API-level, not data-level**. All three must hold — otherwise fix manually, do not retry:
```bash
gh pr view <n> --repo <owner>/<repo> --json mergeable,mergeStateStatus --jq '{mergeable,mergeStateStatus}'
gh pr checks <n> --repo <owner>/<repo>
EXACT_HEAD=$(gh pr view <n> --repo <owner>/<repo> --json headRefOid --jq .headRefOid)
```
Capture `EXACT_HEAD` **once** and reuse it for every retry — SHA pinning makes the loop safe against concurrent pushes.

## Procedure

**1. Monitor GitHub status until recovery**
Poll the components API every ~5 minutes until Actions and Pull Requests are both `operational`:
```bash
curl -s https://www.githubstatus.com/api/v2/components.json \
  | jq -r '.components[] | select(.name=="Actions" or .name=="Pull Requests") | "\(.name): \(.status)"'
```
Run as a detached watcher (bridge-safe-detached-run) if recovery is expected in hours; notify on recovery.

**2. Re-run only failed workflow jobs**
```bash
gh run list --repo <owner>/<repo> --branch <branch> --json databaseId,conclusion \
  --jq '.[] | select(.conclusion=="failure") | .databaseId'
gh run rerun <run-id> --repo <owner>/<repo> --failed
```
Never re-run the full suite during recovery; allow 1–2 min for CI infra to stabilize first.

**3. Watch for completion**
Poll every 30–60 s (`gh pr checks <n> --repo <owner>/<repo> --watch`). If the same workflow fails **twice** after recovery, stop — it is likely a code issue; diagnose instead of auto-retrying.

**4. Merge with GraphQL→REST fallback**
GraphQL fails first and recovers last during incidents. Try normal merge, then fall back to REST with the pinned SHA:
```bash
gh pr merge <n> --repo <owner>/<repo> --squash --delete-branch \
|| gh api -X PUT repos/<owner>/<repo>/pulls/<n>/merge \
     -f merge_method=squash -f sha="$EXACT_HEAD"
```

**5. Retry loop for sustained outages (detached watcher)**
Fixed 3-min interval, max ~2 h (40 attempts). Run via systemd transient unit so it survives bridge/session restarts:
```bash
systemd-run --collect --unit merge-retry-pr<n> \
  --property=StandardOutput=append:/tmp/merge-retry-<n>.log \
  --setenv=HOME="$HOME" --setenv=PATH="$PATH" \
  bash -c 'for i in $(seq 1 40); do
    gh api -X PUT repos/<owner>/<repo>/pulls/<n>/merge -f merge_method=squash -f sha='"$EXACT_HEAD"' && { echo "SUCCESS attempt $i $(date -u +%FT%TZ)"; exit 0; }
    echo "attempt $i failed $(date -u +%FT%TZ)"; sleep 180
  done; echo TIMEOUT; exit 1'
```
Status checks inside the loop should use REST, not GraphQL. Stop on success or timeout; alert at ~1.5 h.

**6. Verify and clean up**
```bash
gh pr view <n> --repo <owner>/<repo> --json merged,mergeCommit
git ls-remote --heads origin <branch>   # expect empty after --delete-branch
```
Confirm the merge commit is on main and no orphaned/queued workflows remain.

## Safety
- **Exact-head SHA pinning**: extract once, reuse for all attempts; reject new pushes during recovery (a push invalidates the pinned SHA — the REST call then fails closed, which is correct).
- **No bypass**: normal merge flow only; never force-push, never admin-bypass checks.
- **Finite timeout**: always cap attempts (~2 h); never infinite retry.
- **Idempotent**: retrying an already-merged PR is a no-op.
- **Credentials**: use `gh` auth / env from `~/.hermes/.env`; never inline tokens in scripts or logs.

## Related Skills
- bridge-safe-detached-run (watcher runtime), gh-ci-wait (CI wait registration), gh-pr-flow (overall PR lifecycle)
