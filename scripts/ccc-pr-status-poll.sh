#!/usr/bin/env bash
# ccc-pr-status-poll — closes the gap identified in ccc-node#962: sessions had
# no push (webhook) or poll path to learn that a PR/issue they opened changed
# state (CI finished, closed as duplicate, merged, ...), so stale claims like
# "PR #906 CI still running" got carried forward across sessions for days
# after the PR was actually closed.
#
# This is the minimal POLL half of that gap (issue #962 proposal 1). It does
# not touch the webhook path (proposal 3, deferred as mid-term infra: gongyung
# has no public inbound endpoint, Tailscale-only) or the session hand-off
# pre-flight revalidation hook (proposal 2, deferred as a complementary
# hardening layer once this proves out).
#
# Tracks each configured "<owner/repo> <author>" pair's OPEN pull requests,
# diffs against the last-seen snapshot, and notifies (spool only — never
# touches the bot token, mirrors ccc-self-update.sh) on:
#   - a check-rollup transition into a terminal state (SUCCESS/FAILURE)
#   - a previously-open PR no longer being open (closed or merged)
# First sighting of a repo/author pair seeds the snapshot silently (no
# notification burst on rollout) since there is nothing to diff against yet.
#
# Config (operator-owned, one line per tracked pair, '#' comments, blank
# lines skipped — mirrors self-update.services):
#   ~/.claude/pr-status-poll.repos     "<owner/repo> <author>" per line
#
# State (this script's own, safe to delete to force a silent re-seed):
#   ~/.claude/state/pr-status-poll.json
#
# Modes: run | status
# Env: CCC_PR_STATUS_POLL_REPOS, CCC_PR_STATUS_POLL_GH (default gh; tests
#      inject a fake), CCC_STATE_DIR, CCC_PUSH_SPOOL, CCC_NODE.
# Exit: 0 = ran cleanly (including "nothing configured" and "nothing changed");
#      3 = locked (another run in progress); other non-zero = aborted.
set -uo pipefail

CLAUDE_DIR="${CCC_CLAUDE_DIR:-${HOME:-/root}/.claude}"
STATE_DIR="${CCC_STATE_DIR:-$CLAUDE_DIR/state}"
LOG="$STATE_DIR/pr-status-poll.log"
LOCK="$STATE_DIR/pr-status-poll.lock"
SPOOL="${CCC_PUSH_SPOOL:-$STATE_DIR/telegram-spool}"
REPOS_FILE="${CCC_PR_STATUS_POLL_REPOS:-$CLAUDE_DIR/pr-status-poll.repos}"
STATE_FILE="${CCC_PR_STATUS_POLL_STATE:-$STATE_DIR/pr-status-poll.json}"
GH="${CCC_PR_STATUS_POLL_GH:-gh}"

mkdir -p "$STATE_DIR" 2>/dev/null

ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() { printf '%s %s\n' "$(ts)" "$*" >> "$LOG" 2>/dev/null; }
say() { printf '%s\n' "$*"; }

notify() { # <text> <dedup-suffix>
  mkdir -p "$SPOOL" 2>/dev/null || return 0
  local node now fname
  node="${CCC_NODE:-$(hostname -s 2>/dev/null || echo node)}"
  now="$(ts)"
  fname="$SPOOL/$(printf '%s' "$now" | tr ':' '-')-PrStatusPoll-$$-${RANDOM:-0}.json"
  jq -nc --arg ts "$now" --arg node "$node" --arg text "$1" --arg d "$2" \
    '{ts:$ts, event:"PrStatusPoll", node:$node, text:$text, dedup:("PrStatusPoll:"+$d)}' \
    > "$fname" 2>/dev/null || rm -f "$fname" 2>/dev/null
}

audit() { # <result> <repos> <transitions> <closed>
  jq -nc --arg ts "$(ts)" --arg result "$1" --argjson repos "$2" \
    --argjson transitions "$3" --argjson closed "$4" \
    '{ts:$ts, result:$result, repos:$repos, transitions:$transitions, closed:$closed}' \
    >> "$LOG" 2>/dev/null
}

# jq filter fragment: derives one overall checkStatus from a PR's
# statusCheckRollup array (mix of CheckRun {status,conclusion} and legacy
# StatusContext {state} entries). status/state PENDING or non-COMPLETED means
# still running. Once everything is terminal, only genuinely bad conclusions
# (FAILURE/CANCELLED/TIMED_OUT/ACTION_REQUIRED/STARTUP_FAILURE/ERROR) count as
# a failure — CheckRun's "NEUTRAL" conclusion (e.g. CodeQL with nothing to
# flag) is a normal completed-and-fine outcome, NOT a reason to keep reporting
# "still pending" (caught via a live smoke test against ccc-node#965, whose
# CodeQL job legitimately concludes NEUTRAL on every green run).
CHECK_STATUS_JQ='
  (.statusCheckRollup // []) as $r
  | if ($r | length) == 0 then "PENDING"
    elif ($r | any((.status // "COMPLETED") != "COMPLETED" or (.state // "") == "PENDING")) then "PENDING"
    elif ($r | any(
        (((.conclusion // "") as $c | ($c == "FAILURE" or $c == "CANCELLED" or $c == "TIMED_OUT" or $c == "ACTION_REQUIRED" or $c == "STARTUP_FAILURE")))
        or (((.state // "") as $s | ($s == "FAILURE" or $s == "ERROR")))
      )) then "FAILURE"
    else "SUCCESS"
    end
'

cmd_status() {
  local n_repos
  n_repos="$(grep -cv '^[[:space:]]*\(#\|$\)' "$REPOS_FILE" 2>/dev/null || echo 0)"
  say "repos file: $REPOS_FILE ($( [ -f "$REPOS_FILE" ] && echo "$n_repos configured" || echo missing ))"
  say "state file: $STATE_FILE ($( [ -f "$STATE_FILE" ] && echo present || echo none ))"
  say "last run:"
  tail -1 "$LOG" 2>/dev/null || say "  (no runs yet)"
}

cmd_run() {
  exec 9>"$LOCK" || { log "lock-open-failed"; exit 4; }
  if ! flock -n 9; then
    log "skip reason=locked"
    say "pr-status-poll: another run is in progress; skipping" >&2
    exit 3
  fi

  local n_repos
  n_repos="$(grep -cv '^[[:space:]]*\(#\|$\)' "$REPOS_FILE" 2>/dev/null || echo 0)"
  if [ ! -f "$REPOS_FILE" ] || [ "$n_repos" -eq 0 ]; then
    log "skip reason=no-repos-file path=$REPOS_FILE"
    say "pr-status-poll: no repos configured ($REPOS_FILE missing/empty); nothing to track" >&2
    exit 0
  fi

  if ! command -v jq >/dev/null 2>&1; then
    log "abort reason=no-jq"
    say "pr-status-poll: jq is required" >&2
    exit 5
  fi

  local prev_state
  prev_state="$(cat "$STATE_FILE" 2>/dev/null || echo '{}')"
  printf '%s' "$prev_state" | jq -e . >/dev/null 2>&1 || prev_state='{}'

  local new_state='{}'
  local repos_seen=0 total_transitions=0 total_closed=0

  while IFS= read -r line; do
    line="${line%%#*}"
    line="$(printf '%s' "$line" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    [ -n "$line" ] || continue
    local repo author
    repo="$(printf '%s' "$line" | awk '{print $1}')"
    author="$(printf '%s' "$line" | awk '{print $2}')"
    if [ -z "$repo" ] || [ -z "$author" ]; then
      log "repo-line skipped reason=invalid-format line=$line"
      continue
    fi
    repos_seen=$((repos_seen + 1))

    local open_raw
    if ! open_raw="$("$GH" pr list --repo "$repo" --author "$author" --state open \
        --json number,url,title,statusCheckRollup,updatedAt 2>>"$LOG")"; then
      log "gh-list-error repo=$repo author=$author"
      # Keep whatever we last knew for this repo rather than dropping tracking
      # on a transient gh/network failure.
      local prev_repo
      prev_repo="$(printf '%s' "$prev_state" | jq -c --arg r "$repo" '.[$r] // {}')"
      new_state="$(printf '%s' "$new_state" | jq -c --arg r "$repo" --argjson v "$prev_repo" '.[$r]=$v')"
      continue
    fi

    local enriched
    enriched="$(printf '%s' "$open_raw" | jq -c '
      map({
        (.number|tostring): {
          checkStatus: ('"$CHECK_STATUS_JQ"'),
          title: .title, url: .url, updatedAt: .updatedAt
        }
      }) | add // {}
    ')"

    local prev_repo
    prev_repo="$(printf '%s' "$prev_state" | jq -c --arg r "$repo" '.[$r] // {}')"

    # Check-rollup transitions into a terminal state, only for PRs we already
    # had a prior snapshot of (first sighting seeds silently).
    local transitions
    transitions="$(printf '%s' "$enriched" | jq -c --argjson prev "$prev_repo" '
      to_entries
      | map(select(
          $prev[.key] != null and
          ($prev[.key].checkStatus // "PENDING") != .value.checkStatus and
          (.value.checkStatus == "SUCCESS" or .value.checkStatus == "FAILURE")
        ))
      | map({number: .key, checkStatus: .value.checkStatus, title: .value.title, url: .value.url})
    ')"
    local t_count
    t_count="$(printf '%s' "$transitions" | jq 'length')"
    total_transitions=$((total_transitions + t_count))
    while IFS= read -r item; do
      [ -n "$item" ] || continue
      local num status title url
      num="$(printf '%s' "$item" | jq -r '.number')"
      status="$(printf '%s' "$item" | jq -r '.checkStatus')"
      title="$(printf '%s' "$item" | jq -r '.title')"
      url="$(printf '%s' "$item" | jq -r '.url')"
      log "transition repo=$repo number=$num checkStatus=$status"
      notify "PR #${num} (${repo}) CI 완료: ${status} — ${title} ${url}" "${repo}:${num}:${status}"
    done < <(printf '%s' "$transitions" | jq -c '.[]')

    # PRs we previously tracked as open that are no longer in the open list —
    # closed or merged since the last run.
    local closed_numbers
    closed_numbers="$(jq -n --argjson prev "$prev_repo" --argjson cur "$enriched" '
      ($prev | keys) - ($cur | keys)
    ')"
    local c_count
    c_count="$(printf '%s' "$closed_numbers" | jq 'length')"
    total_closed=$((total_closed + c_count))
    while IFS= read -r num; do
      [ -n "$num" ] || continue
      local final_json final_state final_title final_url
      final_json="$("$GH" pr view "$num" --repo "$repo" --json state,title,url,mergedAt 2>>"$LOG")" || final_json=""
      if [ -n "$final_json" ]; then
        final_state="$(printf '%s' "$final_json" | jq -r '.state')"
        final_title="$(printf '%s' "$final_json" | jq -r '.title')"
        final_url="$(printf '%s' "$final_json" | jq -r '.url')"
      else
        final_state="UNKNOWN"
        final_title="$(printf '%s' "$prev_repo" | jq -r --arg n "$num" '.[$n].title // ""')"
        final_url="$(printf '%s' "$prev_repo" | jq -r --arg n "$num" '.[$n].url // ""')"
      fi
      log "closed repo=$repo number=$num finalState=$final_state"
      notify "PR #${num} (${repo}) 상태 변경: ${final_state} — ${final_title} ${final_url}" "${repo}:${num}:${final_state}"
    done < <(printf '%s' "$closed_numbers" | jq -r '.[]')

    new_state="$(printf '%s' "$new_state" | jq -c --arg r "$repo" --argjson v "$enriched" '.[$r]=$v')"
  done < "$REPOS_FILE"

  local tmp
  tmp="$STATE_FILE.tmp.$$"
  printf '%s' "$new_state" | jq -c '.' > "$tmp" 2>/dev/null && mv -f "$tmp" "$STATE_FILE" || rm -f "$tmp" 2>/dev/null

  audit "ok" "$repos_seen" "$total_transitions" "$total_closed"
  say "pr-status-poll: ok (repos=$repos_seen transitions=$total_transitions closed=$total_closed)"
  exit 0
}

MODE="${1:-run}"
case "$MODE" in
  run) cmd_run ;;
  status) cmd_status ;;
  *) say "Usage: ccc-pr-status-poll.sh [run|status]" >&2; exit 2 ;;
esac
