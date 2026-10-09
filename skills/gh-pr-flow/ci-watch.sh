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
#               [--required-context <name>]... [--min-checks N] [--settle N]
# --head pins the watch to an exact head (prefix ok): a newer push ends the
# watch with SUPERSEDED instead of reporting a stale rollup as the result.
# GREEN needs every required status context of the PR's base branch present
# and successful, not just "nothing pending" (#2200): right after a push or
# update-branch only the first workflows have registered their checks. The
# contexts are read from branch protection and rulesets; --required-context
# (repeatable) replaces that lookup. When the lookup fails or finds none, GREEN
# must be observed --settle consecutive polls (default 2). --min-checks also
# keeps a smaller rollup RUNNING.
# Exit codes: 0 green · 10 failed (names the failing checks) · 11 merged ·
#   12 closed unmerged · 13 superseded (head moved) · 20 timeout · 2 usage ·
#   3 gh/API error.
# Output: one line per transition (pending/total/missing counts), final verdict
# line (JSON with --json). Body-free: PR number, SHAs, check names only.
set -uo pipefail

REPO=""; PR=""; HEAD=""; INTERVAL=45; TIMEOUT=3600; JSON=0
MIN_CHECKS=1; SETTLE=2; REQ_ARGS=(); REQ_GIVEN=0
while [ $# -gt 0 ]; do
  case "$1" in
    --repo) REPO="${2:-}"; shift 2 ;;
    --pr) PR="${2:-}"; shift 2 ;;
    --head) HEAD="${2:-}"; shift 2 ;;
    --interval) INTERVAL="${2:-}"; shift 2 ;;
    --timeout) TIMEOUT="${2:-}"; shift 2 ;;
    --json) JSON=1; shift ;;
    --required-context) [ -n "${2:-}" ] || { echo "ci-watch: --required-context needs a name" >&2; exit 2; }
      REQ_ARGS+=("$2"); REQ_GIVEN=1; shift 2 ;;
    --min-checks) MIN_CHECKS="${2:-}"; shift 2 ;;
    --settle) SETTLE="${2:-}"; shift 2 ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "ci-watch: unknown argument: $1" >&2; exit 2 ;;
  esac
done
[ -n "$REPO" ] && [ -n "$PR" ] || { echo "ci-watch: --repo and --pr are required" >&2; exit 2; }
case "$PR$INTERVAL$TIMEOUT$MIN_CHECKS$SETTLE" in *[!0-9]*|"") echo "ci-watch: --pr/--interval/--timeout/--min-checks/--settle must be integers" >&2; exit 2 ;; esac
[ "$SETTLE" -ge 1 ] || SETTLE=1
[ "$MIN_CHECKS" -ge 1 ] || MIN_CHECKS=1
case "$HEAD" in *[!0-9a-fA-F]*) echo "ci-watch: --head must be hex" >&2; exit 2 ;; esac
HEAD="$(printf '%s' "$HEAD" | tr 'A-F' 'a-f')"

# Required contexts of the base branch: classic protection (readable without
# admin through the branch endpoint) plus any ruleset required_status_checks.
# Prints a JSON array; returns non-zero when nothing could be read.
required_contexts() {
  local base classic rules
  base="$(gh pr view "$PR" --repo "$REPO" --json baseRefName --jq .baseRefName 2>/dev/null)" || return 1
  [ -n "$base" ] || return 1
  classic="$(gh api "repos/$REPO/branches/$base" \
    --jq '[.protection.required_status_checks.contexts // [] | .[]]' 2>/dev/null)" || classic=""
  rules="$(gh api "repos/$REPO/rules/branches/$base" \
    --jq '[.[]? | select(.type == "required_status_checks") | .parameters.required_status_checks[]?.context]' 2>/dev/null)" || rules=""
  [ -n "$classic$rules" ] || return 1
  jq -cn --argjson a "${classic:-[]}" --argjson b "${rules:-[]}" '$a + $b | unique' 2>/dev/null
}

if [ "$REQ_GIVEN" = 1 ]; then
  REQ_JSON="$(printf '%s\n' "${REQ_ARGS[@]}" | jq -R . | jq -cs 'unique')"
  REQ_SRC="flag"
elif REQ_JSON="$(required_contexts)" && [ -n "$REQ_JSON" ] && [ "$REQ_JSON" != "[]" ]; then
  REQ_SRC="protection"
else
  REQ_JSON="[]"; REQ_SRC="none"
fi
# With a known required set the verdict is already complete on one poll;
# without one, a single "nothing pending" poll may be the registration gap.
NEED_GREEN=1; [ "$REQ_SRC" = none ] && NEED_GREEN="$SETTLE"

# One jq program computes the verdict; the shell only reads tab-separated
# fields (check names contain spaces, e.g. "bridge-tests (3.11)").
JQ='
  def st: (.conclusion // .state // "") | ascii_upcase;
  def is_pending: st as $s | ($s == "" or $s == "PENDING" or $s == "IN_PROGRESS" or $s == "QUEUED" or $s == "WAITING" or $s == "REQUESTED" or $s == "EXPECTED");
  def is_bad: st as $s | ($s == "FAILURE" or $s == "ERROR" or $s == "CANCELLED" or $s == "TIMED_OUT" or $s == "ACTION_REQUIRED" or $s == "STARTUP_FAILURE");
  def nm: (.name // .context // "?");
  '"$REQ_JSON"' as $req
  | (.statusCheckRollup // []) as $r
  | ($r | map(select(is_pending)) | length) as $pending
  | ($r | map(select(is_bad)) | length) as $bad
  | ($r | length) as $total
  | ($r | map(nm)) as $names
  | ($req | map(select(. as $c | ($names | index($c)) == null))) as $missing
  | (if .state == "MERGED" then "MERGED"
     elif .state == "CLOSED" then "CLOSED"
     elif $bad > 0 then "FAILED"
     elif $total >= '"$MIN_CHECKS"' and $pending == 0 and ($missing | length) == 0 then "GREEN"
     else "RUNNING" end) as $verdict
  | [$verdict, (.headRefOid[0:40]), ($pending|tostring), ($bad|tostring), ($total|tostring),
     ($r | map(select(is_bad) | nm) | join(",") | if . == "" then "-" else . end),
     ((.mergeCommit.oid // "-")[0:12]),
     (($missing | length) | tostring),
     ($missing | join(",") | if . == "" then "-" else . end)]
  | join("\t")'

poll() { gh pr view "$PR" --repo "$REPO" --json state,headRefOid,mergeCommit,statusCheckRollup --jq "$JQ" 2>/dev/null; }

emit() { # <verdict> <code> <detail>
  if [ "$JSON" = 1 ]; then
    printf '{"verdict":"%s","pr":%s,"repo":"%s","detail":"%s"}\n' "$1" "$PR" "$REPO" "$(printf '%s' "$3" | tr '"' "'")"
  else
    printf 'ci-watch: %s pr=%s %s\n' "$1" "$PR" "$3"
  fi
  exit "$2"
}

[ "$JSON" = 1 ] || printf 'ci-watch: required pr=%s source=%s contexts=%s\n' "$PR" "$REQ_SRC" "$(jq -r 'length' <<<"$REQ_JSON")"
start=$(date +%s); last=""; misses=0; greens=0
while :; do
  line="$(poll)" || line=""
  if [ -z "$line" ]; then
    misses=$((misses+1)); [ "$misses" -ge 3 ] && emit error 3 "gh pr view failed 3 times in a row"
    sleep 5; continue
  fi
  misses=0
  # fields: verdict head pending bad total failing merge nmissing missing (bad is folded into verdict by jq)
  IFS=$'\t' read -r verdict head pending _bad total failing merge nmissing missing <<<"$line"
  if [ -n "$HEAD" ] && [ "${head#"$HEAD"}" = "$head" ]; then
    emit superseded 13 "watched=$HEAD current=${head:0:12}"
  fi
  case "$verdict" in
    MERGED) emit merged 11 "head=${head:0:12} merge=$merge" ;;
    CLOSED) emit closed 12 "head=${head:0:12}" ;;
    FAILED) emit failed 10 "head=${head:0:12} failing=[$failing] pending=$pending total=$total" ;;
    GREEN)  greens=$((greens+1))
            [ "$greens" -ge "$NEED_GREEN" ] && emit green 0 "head=${head:0:12} total=$total required=$REQ_SRC" ;;
    *)      greens=0 ;;
  esac
  snap="$pending/$total/$nmissing"
  if [ "$snap" != "$last" ]; then
    [ "$JSON" = 1 ] || printf 'ci-watch: running pr=%s head=%s pending=%s total=%s missing=%s\n' "$PR" "${head:0:12}" "$pending" "$total" "$nmissing"
    last="$snap"
  fi
  now=$(date +%s)
  [ $((now - start)) -ge "$TIMEOUT" ] && emit timeout 20 "head=${head:0:12} pending=$pending total=$total missing=[$missing]"
  sleep "$INTERVAL"
done
