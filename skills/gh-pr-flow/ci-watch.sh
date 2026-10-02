#!/usr/bin/env bash
# ci-watch.sh — watch one PR head's check rollup until it is GREEN, FAILED,
# MERGED or CLOSED, and say which. The agent-side fallback for when the
# bridge's durable CI wait (gh-ci-wait) cannot be registered.
#
# Why a script: on 2026-10-02 two hand-rolled "poll until green" loops never
# fired. Their exit test grepped for '"pending":0,"bad":0,"total":N' in gh's
# --jq output, but gh sorts object keys alphabetically, so the pattern could
# not occur; the loops reacted to FAILED and MERGED only and the operator
# noticed the green PRs first (#2113, #2120). This script derives one verdict
# word inside jq and never inspects key order.
#
# Usage:
#   ci-watch.sh --repo <owner/repo> --pr <n> [--head <sha-prefix>]
#               [--interval 45] [--timeout 3600] [--json]
# --head pins the watch to an exact head (prefix ok): a newer push ends the
# watch with SUPERSEDED instead of reporting a stale rollup as the result.
# Exit codes: 0 green · 10 failed (names the failing checks) · 11 merged ·
#   12 closed unmerged · 13 superseded (head moved) · 20 timeout · 2 usage ·
#   3 gh/API error.
# Output: one line per transition (pending/total counts), final verdict line
# (JSON with --json). Body-free: PR number, SHAs, check names only.
set -uo pipefail

REPO=""; PR=""; HEAD=""; INTERVAL=45; TIMEOUT=3600; JSON=0
while [ $# -gt 0 ]; do
  case "$1" in
    --repo) REPO="${2:-}"; shift 2 ;;
    --pr) PR="${2:-}"; shift 2 ;;
    --head) HEAD="${2:-}"; shift 2 ;;
    --interval) INTERVAL="${2:-}"; shift 2 ;;
    --timeout) TIMEOUT="${2:-}"; shift 2 ;;
    --json) JSON=1; shift ;;
    -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
    *) echo "ci-watch: unknown argument: $1" >&2; exit 2 ;;
  esac
done
[ -n "$REPO" ] && [ -n "$PR" ] || { echo "ci-watch: --repo and --pr are required" >&2; exit 2; }
case "$PR$INTERVAL$TIMEOUT" in *[!0-9]*) echo "ci-watch: --pr/--interval/--timeout must be integers" >&2; exit 2 ;; esac
case "$HEAD" in *[!0-9a-fA-F]*) echo "ci-watch: --head must be hex" >&2; exit 2 ;; esac
HEAD="$(printf '%s' "$HEAD" | tr 'A-F' 'a-f')"

# One jq program computes the verdict; the shell only reads whitespace-separated fields.
JQ='
  def st: (.conclusion // .state // "") | ascii_upcase;
  def is_pending: st as $s | ($s == "" or $s == "PENDING" or $s == "IN_PROGRESS" or $s == "QUEUED" or $s == "WAITING" or $s == "REQUESTED" or $s == "EXPECTED");
  def is_bad: st as $s | ($s == "FAILURE" or $s == "ERROR" or $s == "CANCELLED" or $s == "TIMED_OUT" or $s == "ACTION_REQUIRED" or $s == "STARTUP_FAILURE");
  (.statusCheckRollup // []) as $r
  | ($r | map(select(is_pending)) | length) as $pending
  | ($r | map(select(is_bad)) | length) as $bad
  | ($r | length) as $total
  | (if .state == "MERGED" then "MERGED"
     elif .state == "CLOSED" then "CLOSED"
     elif $bad > 0 then "FAILED"
     elif $total > 0 and $pending == 0 then "GREEN"
     else "RUNNING" end) as $verdict
  | [$verdict, (.headRefOid[0:40]), ($pending|tostring), ($bad|tostring), ($total|tostring),
     ($r | map(select(is_bad) | (.name // .context // "?")) | join(",") | if . == "" then "-" else . end),
     ((.mergeCommit.oid // "-")[0:12])]
  | join(" ")'

poll() { gh pr view "$PR" --repo "$REPO" --json state,headRefOid,mergeCommit,statusCheckRollup --jq "$JQ" 2>/dev/null; }

emit() { # <verdict> <code> <detail>
  if [ "$JSON" = 1 ]; then
    printf '{"verdict":"%s","pr":%s,"repo":"%s","detail":"%s"}\n' "$1" "$PR" "$REPO" "$(printf '%s' "$3" | tr '"' "'")"
  else
    printf 'ci-watch: %s pr=%s %s\n' "$1" "$PR" "$3"
  fi
  exit "$2"
}

start=$(date +%s); last=""; misses=0
while :; do
  line="$(poll)" || line=""
  if [ -z "$line" ]; then
    misses=$((misses+1)); [ "$misses" -ge 3 ] && emit error 3 "gh pr view failed 3 times in a row"
    sleep 5; continue
  fi
  misses=0
  set -- $line; verdict="$1"; head="$2"; pending="$3"; bad="$4"; total="$5"; failing="$6"; merge="$7"
  if [ -n "$HEAD" ] && [ "${head#"$HEAD"}" = "$head" ]; then
    emit superseded 13 "watched=$HEAD current=${head:0:12}"
  fi
  case "$verdict" in
    MERGED) emit merged 11 "head=${head:0:12} merge=$merge" ;;
    CLOSED) emit closed 12 "head=${head:0:12}" ;;
    FAILED) emit failed 10 "head=${head:0:12} failing=[$failing] pending=$pending total=$total" ;;
    GREEN)  emit green 0 "head=${head:0:12} total=$total" ;;
  esac
  snap="$pending/$total"
  if [ "$snap" != "$last" ]; then
    [ "$JSON" = 1 ] || printf 'ci-watch: running pr=%s head=%s pending=%s total=%s\n' "$PR" "${head:0:12}" "$pending" "$total"
    last="$snap"
  fi
  now=$(date +%s)
  [ $((now - start)) -ge "$TIMEOUT" ] && emit timeout 20 "head=${head:0:12} pending=$pending total=$total"
  sleep "$INTERVAL"
done
