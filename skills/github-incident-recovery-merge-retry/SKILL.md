---
name: github-incident-recovery-merge-retry
description: Recover a green, mergeable PR through a GitHub infrastructure outage — poll the githubstatus components API until Actions/Pull Requests recover, re-run only failed workflow jobs, then land the PR with an exact-head (SHA-pinned) squash merge that falls back from GraphQL to REST and retries on a fixed interval via a detached background watcher. Use when GitHub returns 5xx/timeouts on CI or merge writes while the PR itself has no code problem. Established during the 2026-08-17 GitHub incident (jinwon-int/nclex #210 campaign).
---

## When to Use
- GitHub Actions or PR/API components are degraded (5xx, timeouts) and workflow runs failed for infra reasons, not code
- A merge attempt returns 503/transient errors while the PR is green, mergeable, and conflict-free
- You need to land the PR without manual escalation, possibly across session boundaries

## Preconditions (validate BEFORE registering any watcher)
Failure must be **API-level, not data-level**. Run all three, then compare against the gate table — the commands alone decide nothing without their accepted outputs:
```bash
gh pr view <n> --repo <owner>/<repo> --json mergeable,mergeStateStatus --jq '{mergeable,mergeStateStatus}'
gh pr checks <n> --repo <owner>/<repo>
EXACT_HEAD=$(gh pr view <n> --repo <owner>/<repo> --json headRefOid --jq .headRefOid)
```

Gate table (`mergeable` here is the GraphQL tri-state, not the REST boolean):

| Reading | Verdict |
|---|---|
| `mergeable: MERGEABLE` + `mergeStateStatus: CLEAN` | **Accept** — proceed |
| `mergeable: MERGEABLE` + `mergeStateStatus: HAS_HOOKS` | Accept only if the pending hook/automation is expected |
| `mergeable: CONFLICTING` | **Reject** — data-level; resolve the conflict manually |
| `mergeStateStatus: DIRTY` | **Reject** — merge conflict |
| `mergeStateStatus: BLOCKED` | **Reject** — missing required review/check; satisfy the gate manually |
| `mergeStateStatus: UNSTABLE` | **Reject** — checks failing; go to step 2 |
| `mergeStateStatus: BEHIND` | **Reject** — update the branch and let CI re-run |
| `mergeable: UNKNOWN` | **No verdict yet** — mergeability is computed asynchronously, and this is common precisely during degradation. Wait 30–60 s and re-query, up to 3 times. If it still will not settle, use the local ground-truth procedure in `github-merge-state-conflict-diagnosis` §3 and record that evidence before proceeding |

Required checks must be green **on `EXACT_HEAD` itself** (step 2 filters runs by head SHA for this reason — a green run on an older commit of the same branch proves nothing about the pinned head).
Capture `EXACT_HEAD` **once** and reuse it for every retry — SHA pinning makes the loop safe against concurrent pushes.

## Procedure

**1. Monitor GitHub status until recovery**
Poll the components API every ~5 minutes until Actions and Pull Requests are both `operational`:
```bash
curl -s https://www.githubstatus.com/api/v2/components.json \
  | jq -r '.components[] | select(.name=="Actions" or .name=="Pull Requests") | "\(.name): \(.status)"'
```
Run as a detached watcher (bridge-safe-detached-run) if recovery is expected in hours; notify on recovery.

**2. Re-run only failed workflow jobs on the pinned head**

Filter by head SHA, not by branch alone — a branch selector also matches runs
from earlier commits, so an unpinned rerun can resurrect stale failures unrelated
to `EXACT_HEAD` (`headSha` is a valid `gh run list --json` field; re-verify with
`gh run list --help` on your gh build):
```bash
gh run list --repo <owner>/<repo> --branch <branch> --json databaseId,conclusion,headSha \
  --jq ".[] | select(.conclusion==\"failure\" and .headSha==\"$EXACT_HEAD\") | .databaseId"
gh run rerun <run-id> --repo <owner>/<repo> --failed
```
Never re-run the full suite during recovery; allow 1–2 min for CI infra to stabilize first.

**3. Watch for completion**
Poll every 30–60 s (`gh pr checks <n> --repo <owner>/<repo> --watch`). If the same workflow fails **twice** after recovery, stop — it is likely a code issue; diagnose instead of auto-retrying.

**4. Merge — queue-aware enqueue, or direct exact-head merge**

"GraphQL fails first and recovers last during incidents" is a dated observation
from the single 2026-08-17 incident (n=1), not an invariant — check
`https://www.githubstatus.com/history` before leaning on the ordering. The
fallback order itself is justified fail-safe regardless: both paths below fail
closed on a moved head.

**Merge queue first.** If the repository requires a merge queue, the watcher
enqueues and verifies by readback instead of direct-merging:
```bash
gh pr merge <n> --repo <owner>/<repo> --squash    # enqueues when a queue is required
```
- Record `EXACT_HEAD` as the expected head **at enqueue time** — that is the pin
  the eventual queue merge must be judged against in step 6.
- **Queue admission is not MERGED.** The PR stays `OPEN` after a successful
  enqueue; never treat the enqueue result (or `--auto` enablement) as a landed
  merge. Poll step 6 until `state == "MERGED"`.
- **No `--delete-branch` for a queue merge**: the queue owns branch handling, and
  deleting the head from the watcher side races it.

**Direct merge (no queue)** — both branches must honour the exact-head pin:
```bash
gh pr merge <n> --repo <owner>/<repo> --squash --delete-branch \
     --match-head-commit "$EXACT_HEAD" \
|| gh api -X PUT repos/<owner>/<repo>/pulls/<n>/merge \
     -f merge_method=squash -f sha="$EXACT_HEAD"
```

`--match-head-commit` is what makes the first branch honour the Safety
section's exact-head guarantee. Without it the CLI path merges whatever the
head happens to be, so a push that lands mid-recovery would be merged
unreviewed while the REST fallback right beside it would correctly fail
closed — the two branches must agree.

**5. Retry loop for sustained outages (detached watcher)**
Fixed 3-min interval, max ~2 h (40 attempts). Run via systemd transient unit so it survives bridge/session restarts.

**Credential preflight before registering the unit.** The transient unit starts
with only the environment you give it: a `gh` that authenticates from its own
credential store (hosts.yml/keyring under `$HOME`) keeps working, while a `gh`
that authenticates from a node env file starts with **no token** and burns all
40 attempts on auth failures. Probe auth exactly as the unit will see it, and
stop if it fails:
```bash
gh auth status -h github.com   # with the same $HOME the unit inherits; must pass with no token in argv
```
If auth comes from an env file, make the unit **source the resolved env file
itself** as its first lines (`set -a; . /path/to/env; set +a`) — a file read,
not a token on argv and not a copy. Never pass a token via `--setenv=`
(`systemd-run` argv is visible to other local users) and never write a token to
a new location.

**Classification: only transport/5xx failures are retryable.** 401/403
(auth/permission), 404, 405 (not mergeable), 409 (head moved) and 422
(validation) are decisions — stop and classify with a state readback, do not
spend the retry budget on them:
```bash
systemd-run --collect --unit merge-retry-pr<n> \
  --property=StandardOutput=append:/tmp/merge-retry-<n>.log \
  --setenv=HOME="$HOME" --setenv=PATH="$PATH" \
  bash -c 'set -a; [ -f /path/to/env ] && . /path/to/env; set +a
    for i in $(seq 1 40); do
      ST="$(gh pr view <n> --repo <owner>/<repo> --json state 2>&1)" \
        || { echo "READBACK FAIL attempt $i $(date -u +%FT%TZ)"; sleep 180; continue; }
      case "$ST" in *"MERGED"*) echo "ALREADY MERGED attempt $i $(date -u +%FT%TZ)"; exit 0 ;; esac
      OUT="$(gh api -X PUT repos/<owner>/<repo>/pulls/<n>/merge -f merge_method=squash -f sha='"$EXACT_HEAD"' 2>&1)" \
        && { echo "SUCCESS attempt $i $(date -u +%FT%TZ)"; exit 0; }
      case "$OUT" in
        *"HTTP 409"*) echo "STOP attempt $i: head moved (409) $(date -u +%FT%TZ)"; exit 2 ;;
        *"HTTP 405"*) echo "STOP attempt $i: not mergeable (405) $(date -u +%FT%TZ)"; exit 3 ;;
        *"HTTP 401"*|*"HTTP 403"*) echo "STOP attempt $i: auth/permission (401/403) $(date -u +%FT%TZ)"; exit 4 ;;
        *"HTTP 404"*) echo "STOP attempt $i: PR not found (404) $(date -u +%FT%TZ)"; exit 5 ;;
        *"HTTP 422"*) echo "STOP attempt $i: validation (422) $(date -u +%FT%TZ)"; exit 6 ;;
      esac
      echo "attempt $i retryable (transport/5xx) $(date -u +%FT%TZ)"; sleep 180
    done; echo TIMEOUT; exit 1'
```
The pre-attempt state readback also covers the lost-response case (a merge that
succeeded but whose response was lost is detected as MERGED on the next pass
instead of being retried to a false TIMEOUT) and prevents pointless merge writes
to a PR that was closed or merged while the watcher slept. Status checks inside
the loop use REST, not GraphQL. Stop on success, any decision-class failure, or
timeout; alert at ~1.5 h.

**Retry only what an outage causes.** 409 and 405 are decisions, not transport
failures — retrying them for two hours only delays the alert. Measured against
the live API on 2026-09-10:

| Situation | Result |
|---|---|
| Already-merged PR, correct pinned SHA | **200** `{"merged": true}` — genuinely idempotent |
| Already-merged PR, wrong SHA | **200** `{"merged": true}` — the SHA is not re-checked once merged |
| Open PR, SHA no longer the head | **409** `Head branch was modified` |

So the "idempotent" claim in Safety holds for the case that matters — a
retry after a merge that already succeeded is a no-op — but it does not
license retrying every failure. Re-measure before trusting this table; GitHub
can change these responses.

**6. Verify by readback — the readback is the merge-success evidence**
```bash
gh pr view <n> --repo <owner>/<repo> --json state,mergedAt,mergeCommit \
  --jq '{state,mergedAt,oid:.mergeCommit.oid}'
git ls-remote --heads origin <branch>
```
`merged` is **not** a valid `gh pr view --json` field (`Unknown JSON field:
"merged"`; re-measured 2026-09-13 on gh 2.93.0 — re-verify on your build before
depending on it). Success is `state == "MERGED"` plus `mergedAt`/`mergeCommit`
readback. The branch expectation is conditional on which path landed the merge:
- direct merge via the CLI branch (`--delete-branch`) → head branch gone,
  `ls-remote` output empty
- REST fallback → head branch **remains** (REST does not delete it); delete it
  explicitly only if policy requires
  (`gh api -X DELETE repos/<owner>/<repo>/git/refs/heads/<branch>`), otherwise
  expect it non-empty — the unconditional empty-output assertion is a false
  failure on this path
- merge queue → do **not** touch the head branch from the watcher; the queue owns it

Confirm the merge commit is on main and no orphaned/queued workflows remain.

## Safety
- **Exact-head SHA pinning**: extract once, reuse for all attempts; reject new pushes during recovery (a push invalidates the pinned SHA — the REST call then fails closed, which is correct).
- **No bypass**: normal merge flow only; never force-push, never admin-bypass checks.
- **Finite timeout**: always cap attempts (~2 h); never infinite retry.
- **Idempotent where it counts**: retrying the merge call on an already-merged PR returns 200 `{"merged": true}`, so a retry after a merge that already landed is harmless. This is *not* a licence to retry every failure — only transport/5xx errors are retryable; 401/403 (auth/permission), 404, 405 (not mergeable), 409 (head moved) and 422 (validation) are decisions and must stop the loop with a state readback, not consume its budget. See the status table in step 5.
- **Exact author/reviewer separation**: the actor running steps 4–6 must not be the PR author. Before registering any watcher, read back and confirm an independent reviewer approved this exact head:
  `gh pr view <n> --repo <owner>/<repo> --json author,latestReviews --jq '{author:.author.login, reviews:[.latestReviews[]|{by:.author.login,state}]}'`
  — the reviewer login must differ from the author login with state `APPROVED`. A moved head invalidates both the pin and any stale approval; exact-head pinning without reviewer separation only automates self-merging.
- **Credentials**: prefer `gh`'s own credential store (hosts.yml/keyring under `$HOME`). Env-file auth is allowed only if the watcher unit sources the resolved env file itself (resolve the path on your node, don't assume) and the step-5 preflight proves the unit can authenticate; never inline tokens in scripts or logs, never pass them on argv or `--setenv=`, and never copy them to a new location.

## Related Skills
- bridge-safe-detached-run (watcher runtime), gh-ci-wait (CI wait registration), gh-pr-flow (overall PR lifecycle)
