#!/usr/bin/env bash
# merge-queue-watch.sh — watch an enqueued PR until it MERGES or is EVICTED.
#
# A watch that only polls for state=MERGED is blind to the queue's failure
# path: when the speculative group run fails, GitHub silently drops the entry
# and the PR goes back to OPEN/CLEAN with its approval intact — nothing on the
# PR itself changes, so "wait for MERGED" waits forever (observed 2026-10-02
# on ccc-node#2113, evicted by a flaky shard test; the operator noticed first).
#
# This script polls BOTH the PR state and the repo's merge-queue entries and
# exits as soon as either is terminal. On eviction it names the failed group
# run and its failing jobs so the next step (retry vs fix) is a decision, not
# an investigation.
#
# Usage:
#   merge-queue-watch.sh --repo <owner/repo> --pr <n> [--branch main]
#                        [--interval 45] [--timeout 3600] [--json]
# Exit codes:
#   0 merged · 10 evicted (queue entry gone, PR still open) · 11 closed
#   unmerged · 12 never enqueued (no entry on first poll) · 20 timeout ·
#   2 usage · 3 gh/API error
# Output: one line per transition; final line is the verdict (JSON with --json).
# Body-free: only PR number, state, SHAs, run/job names and ids are printed.
set -uo pipefail

REPO=""; PR=""; BRANCH="main"; INTERVAL=45; TIMEOUT=3600; JSON=0
while [ $# -gt 0 ]; do
  case "$1" in
    --repo) REPO="${2:-}"; shift 2 ;;
    --pr) PR="${2:-}"; shift 2 ;;
    --branch) BRANCH="${2:-}"; shift 2 ;;
    --interval) INTERVAL="${2:-}"; shift 2 ;;
    --timeout) TIMEOUT="${2:-}"; shift 2 ;;
    --json) JSON=1; shift ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "merge-queue-watch: unknown argument: $1" >&2; exit 2 ;;
  esac
done
[ -n "$REPO" ] && [ -n "$PR" ] || { echo "merge-queue-watch: --repo and --pr are required" >&2; exit 2; }
case "$PR$INTERVAL$TIMEOUT" in *[!0-9]*) echo "merge-queue-watch: --pr/--interval/--timeout must be integers" >&2; exit 2 ;; esac
OWNER="${REPO%%/*}"; NAME="${REPO#*/}"

pr_state() { # prints: <state> <mergeStateStatus> <headSha12> <mergeSha12>
  gh pr view "$PR" --repo "$REPO" --json state,mergeStateStatus,headRefOid,mergeCommit \
    --jq '"\(.state) \(.mergeStateStatus) \(.headRefOid[0:12]) \((.mergeCommit.oid//"-")[0:12])"' 2>/dev/null
}
queue_entry() { # prints: <state> <position> or empty when the PR is not queued
  gh api graphql \
    -f query='query($o:String!,$n:String!,$b:String!){repository(owner:$o,name:$n){mergeQueue(branch:$b){entries(first:50){nodes{pullRequest{number} state position}}}}}' \
    -f o="$OWNER" -f n="$NAME" -f b="$BRANCH" \
    --jq ".data.repository.mergeQueue.entries.nodes[]|select(.pullRequest.number==$PR)|\"\(.state) \(.position)\"" 2>/dev/null
}
failed_group_runs() { # prints: <runId> <workflow> <conclusion> per failed run on the PR's group ref(s)
  gh run list --repo "$REPO" --limit 40 --json databaseId,headBranch,name,conclusion \
    --jq ".[]|select(.headBranch|test(\"^gh-readonly-queue/$BRANCH/pr-$PR-\"))|select(.conclusion==\"failure\" or .conclusion==\"cancelled\" or .conclusion==\"timed_out\")|\"\(.databaseId) \(.name) \(.conclusion)\"" 2>/dev/null
}
failed_jobs() { # <runId> → prints: <jobId> <jobName>
  gh api "repos/$REPO/actions/runs/$1/jobs?per_page=50" \
    --jq '.jobs[]|select(.conclusion=="failure" or .conclusion=="cancelled" or .conclusion=="timed_out")|"\(.id) \(.name)"' 2>/dev/null
}

emit() { # <verdict> <code> [detail...]
  local verdict="$1" code="$2"; shift 2
  if [ "$JSON" = 1 ]; then
    printf '{"verdict":"%s","pr":%s,"repo":"%s","detail":"%s"}\n' "$verdict" "$PR" "$REPO" "$(printf '%s' "$*" | tr '"' "'" | tr '\n' ' ')"
  else
    printf 'merge-queue-watch: %s pr=%s %s\n' "$verdict" "$PR" "$*"
  fi
  exit "$code"
}

start=$(date +%s); last_q=""; last_pr=""; first=1
while :; do
  pr="$(pr_state)" || pr=""
  [ -n "$pr" ] || { sleep 5; pr="$(pr_state)" || pr=""; [ -n "$pr" ] || emit error 3 "gh pr view failed"; }
  set -- $pr; state="$1"; ms="$2"; head="$3"; merge="$4"
  case "$state" in
    MERGED) emit merged 0 "merge=$merge head=$head" ;;
    CLOSED) emit closed 11 "head=$head" ;;
  esac
  q="$(queue_entry)" || q=""
  if [ -z "$q" ]; then
    if [ "$first" = 1 ]; then emit not-enqueued 12 "head=$head mergeStateStatus=$ms"; fi
    detail="head=$head mergeStateStatus=$ms"
    runs="$(failed_group_runs)"
    if [ -n "$runs" ]; then
      while read -r rid wf concl; do
        [ -n "$rid" ] || continue
        jobs="$(failed_jobs "$rid" | tr '\n' ';')"
        detail="$detail | run=$rid $wf=$concl jobs=[${jobs%;}]"
      done <<< "$runs"
    fi
    emit evicted 10 "$detail"
  fi
  first=0
  if [ "$q" != "$last_q" ] || [ "$pr" != "$last_pr" ]; then
    [ "$JSON" = 1 ] || printf 'merge-queue-watch: queued pr=%s entry=%s head=%s mergeStateStatus=%s\n' "$PR" "$q" "$head" "$ms"
    last_q="$q"; last_pr="$pr"
  fi
  now=$(date +%s)
  [ $((now - start)) -ge "$TIMEOUT" ] && emit timeout 20 "entry=$q head=$head"
  sleep "$INTERVAL"
done
