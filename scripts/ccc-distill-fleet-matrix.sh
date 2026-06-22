#!/usr/bin/env bash
# ccc-distill-fleet-matrix.sh — read-only #82 fleet closure matrix builder.
#
# Parses fleet evidence files (read-only `bash scripts/ccc-distill-check.sh`
# snapshots + per-node path probes) into a structured JSON closure matrix that
# the finalizer can use to decide whether #82 acceptance is met or whether
# per-node blocker subissues should be opened.
#
# No network, no mutations, no Honcho sends. POSIX-awk safe (mawk friendly).
#
# Usage:
#   bash scripts/ccc-distill-fleet-matrix.sh \
#     --status /path/to/fleet-status.txt \
#     --path-probe /path/to/fleet-path-probe.txt \
#     [--target-commit 036c230947e2d2f92af2188d0707e5b0b0c5b268] \
#     [--node-list seoseo,gwakga,bangtong,sogyo,gongyung,nosuk,soonwook,yukson,daegyo,dungae] \
#     > /tmp/ccc-fleet-matrix.json
#
# Output: single JSON object on stdout.
#
# Evidence file formats (matching the r27 collector output):
#   Status file:
#     ===== <node> =====
#     HOST=<host>
#     NO_CHECKER_FOUND
#     (blank line separates blocks)
#
#   Path probe file:
#     ===== <node> =====
#     HOST=<host>
#     CANDIDATE=<path>
#     <git_remote_url>      # unkeyed; first unkeyed https line
#     <commit_short_sha>    # unkeyed; first 7-40 hex line after the URL
#     NO_CHECKER_FOUND
set -uo pipefail

usage() {
  sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

TARGET_COMMIT="036c230947e2d2f92af2188d0707e5b0b0c5b268"
KNOWN_NODES="seoseo,gwakga,bangtong,sogyo,gongyung,nosuk,soonwook,yukson,daegyo,dungae"
STATUS_FILE=""
PROBE_FILE=""

while [ $# -gt 0 ]; do
  case "$1" in
    --status)        STATUS_FILE="${2:-}"; shift 2 ;;
    --path-probe)    PROBE_FILE="${2:-}"; shift 2 ;;
    --target-commit) TARGET_COMMIT="${2:-}"; shift 2 ;;
    --node-list)     KNOWN_NODES="${2:-}"; shift 2 ;;
    -h|--help)       usage 0 ;;
    *)               printf 'unknown arg: %s\n' "$1" >&2; usage 2 ;;
  esac
done

if [ -z "$STATUS_FILE" ] && [ -z "$PROBE_FILE" ]; then
  printf 'at least one of --status or --path-probe is required\n' >&2
  exit 2
fi

# split_file <file>
#   Reads a fleet evidence file and emits one record per node on stdout.
#   The record format is:
#     <node>\x1f<host>\x1f<extra1>\x1f<extra2>...
#   `\x1f` is the ASCII Unit Separator (US), a control character that
#   cannot appear in node names, host names, URLs, paths, or commit SHAs.
#   This avoids bash's empty-field elision when a node has no HOST= line:
#   bash's `read` collapses runs of whitespace separators, but `\x1f` is
#   non-whitespace and survives.
#
#   POSIX-portable: mawk-friendly. The global `lines` array is intentionally
#   not declared as a function-local parameter; awk treats such locals as a
#   separate array, which silently dropped records in an earlier draft.
split_file() {
  awk -v RS='' -F'\n' '
    function flush(name, host, n,    i) {
      if (name == "") return
      printf "%s\x1f%s", name, host
      for (i = 1; i <= n; i++) {
        printf "\x1f%s", lines[i]
      }
      printf "\n"
    }
    {
      delete lines
      name = ""; host = ""; n = 0
      for (i = 1; i <= NF; i++) {
        raw = $i
        sub(/\r$/, "", raw)
        if (raw ~ /^===== /) {
          if (name != "") flush(name, host, n)
          delete lines
          n = 0
          n2 = split(raw, parts, " ")
          name = parts[2]
          host = ""
          continue
        }
        if (raw ~ /^HOST=/) {
          host = substr(raw, 6)
          continue
        }
        if (raw ~ /^CANDIDATE=/) {
          n++
          lines[n] = raw
          continue
        }
        if (raw == "") continue
        n++
        lines[n] = raw
      }
      if (name != "") flush(name, host, n)
    }
  ' "$1"
}

# tmp file to merge records
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT
MERGED="$TMP_DIR/merged.tsv"
: > "$MERGED"

declare -A HOST    # node -> host
declare -A CAND    # node -> candidate path
declare -A URL     # node -> git url
declare -A COMMIT  # node -> short commit
declare -A STATUS  # node -> raw status string

ingest() {
  local file="$1"
  [ -z "$file" ] && return 0
  [ -f "$file" ] || return 0
  local name host extra rec
  while IFS= read -r rec <&3; do
    # rec is `<node>\x1f<host>\x1f<extra...>`; split into name/host/extras.
    name="${rec%%$'\x1f'*}"; rest="${rec#*$'\x1f'}"
    host="${rest%%$'\x1f'*}"; extra=""
    if [ "$rest" != "$host" ]; then
      # there is more after host
      tmp="${rest#*$'\x1f'}"
      extra="${tmp}"
    fi
    [ -z "$name" ] && continue
    [ -n "$host" ] && HOST["$name"]="$host"
    # Parse extras. First https://... line wins for URL; first 7-40 hex line for commit.
    local field
    for field in $extra; do
      case "$field" in
        CANDIDATE=*) CAND["$name"]="${field#CANDIDATE=}" ;;
        NO_CHECKER_FOUND) STATUS["$name"]="NO_CHECKER_FOUND" ;;
        ssh:*|Connection*|connect*|timed*out*)
          STATUS["$name"]="UNREACHABLE"
          ;;
        https://*|git@*)
          [ -z "${URL[$name]:-}" ] && URL["$name"]="$field"
          ;;
        *)
          # 7-40 hex chars = short sha
          if printf '%s' "$field" | grep -Eq '^[0-9a-f]{7,40}$'; then
            [ -z "${COMMIT[$name]:-}" ] && COMMIT["$name"]="$field"
          fi
          ;;
      esac
    done
  done 3< <(split_file "$file")
}

ingest "$STATUS_FILE"
ingest "$PROBE_FILE"

# Some nodes may have only a path-probe entry and no status entry; mark absent
# status as "no_evidence" so the finalizer can see the gap.

# Build per-node JSON entries via jq.
TARGET_SHORT="$(printf '%s' "$TARGET_COMMIT" | cut -c1-7)"

# Default verification logic (in jq):
#   verified     -> status missing (i.e. checker found & reported OK) AND commit >= target
#   blocked      -> NO_CHECKER_FOUND, unreachable, behind target, or no evidence
#   pending      -> (reserved; not used by this round)

# We use jq to build the node array from a TSV passed via --arg pairs.

# Encode arrays via printf-and-jq --arg (handles arbitrary strings safely).

# Compose nodes by iterating the known node list (order preserved).
NODES_JSON=""
first=1
IFS=',' read -ra NODE_ARR <<< "$KNOWN_NODES"
for node in "${NODE_ARR[@]}"; do
  host_val="${HOST[$node]:-}"
  cand_val="${CAND[$node]:-}"
  url_val="${URL[$node]:-}"
  commit_val="${COMMIT[$node]:-}"
  status_val="${STATUS[$node]:-}"
  [ -z "$status_val" ] && status_val="no_evidence"

  # Determine mode / checker_available / blocker in jq for clarity.
  obj="$(jq -nc \
    --arg name "$node" \
    --arg host "$host_val" \
    --arg candidate "$cand_val" \
    --arg url "$url_val" \
    --arg commit "$commit_val" \
    --arg status "$status_val" \
    --arg target "$TARGET_COMMIT" \
    --arg target_short "$TARGET_SHORT" '
    def is_hex: test("^[0-9a-f]+$");
    def is_short7: (length == 7 and is_hex);
    def is_full40: (length == 40 and is_hex);
    def detect_short_c:
      if ($commit | is_short7) then $commit
      elif ($commit | is_full40) then $commit[0:7]
      else null
      end;
    def cmp_to($t):
      if . == null then null
      elif . == $t then "equal"
      else "behind"
      end;
    def state_for($st):
      if $st == "NO_CHECKER_FOUND" then
        {checker_available: false, mode: "missing", verification: "blocked",
         blocker_reason: "checker_not_found_at_candidate_path"}
      elif $st == "UNREACHABLE" then
        {checker_available: false, mode: "unreachable", verification: "blocked",
         blocker_reason: "node_unreachable_over_ssh"}
      elif $st == "no_evidence" then
        {checker_available: false, mode: "unknown", verification: "blocked",
         blocker_reason: "no_evidence_in_probe"}
      else
        {checker_available: true, mode: "unknown", verification: "pending",
         blocker_reason: null}
      end;
    detect_short_c as $c
    | ($c | cmp_to($target_short)) as $cmp
    | state_for($status) as $state
    | {
        name: $name,
        host: (if $host == "" then null else $host end),
        candidate: (if $candidate == "" then null else $candidate end),
        git_url: (if $url == "" then null else $url end),
        probe_commit: (if $commit == "" then null else $commit end),
        behind_target: (if $cmp == "behind" then true elif $cmp == "equal" then false else null end),
        commit_compare: $cmp,
        target_commit_short: $target_short,
        status: $status,
        checker_available: $state.checker_available,
        mode: $state.mode,
        verification: $state.verification,
        blocker_reason: $state.blocker_reason
      }
  ')"

  if [ "$first" = 1 ]; then
    NODES_JSON="$obj"
    first=0
  else
    NODES_JSON="$NODES_JSON,$obj"
  fi
done

# Summary roll-up via jq.
SUMMARY="$(jq -nc \
  --argjson nodes "[$NODES_JSON]" '
  ($nodes | map(select(.verification == "verified")) | length) as $v
  | ($nodes | map(select(.verification == "blocked"))  | length) as $b
  | ($nodes | map(select(.verification == "pending"))  | length) as $p
  | ($nodes | map(select(.checker_available == true))  | length) as $ck
  | ($nodes | map(select(.behind_target == true))      | length) as $bt
  | ($nodes | map(select(.status == "UNREACHABLE"))    | length) as $ur
  | ($nodes | map(select(.status == "NO_CHECKER_FOUND")) | length) as $nc
  | {
      total: ($nodes | length),
      verified: $v,
      blocked: $b,
      pending: $p,
      checker_available: $ck,
      behind_target: $bt,
      unreachable: $ur,
      no_checker_found: $nc
    }
')"

# Recommended subissues: one per blocked node whose blocker is real (not "no_evidence").
SUB_JSON="$(jq -nc \
  --argjson nodes "[$NODES_JSON]" \
  --arg issue "82" '
  [ $nodes[]
    | select(.verification == "blocked")
    | {
        node: .name,
        host: .host,
        reason: .blocker_reason,
        behind_target: .behind_target,
        suggested_title: ("#\($issue) fleet verification: " + .name),
        suggested_body: (
          "Fleet rollout #\($issue) is blocked on the " + .name + " node.\n" +
          "Reason: " + (.blocker_reason // "unknown") + ".\n" +
          "Host: " + (.host // "unknown") + ".\n" +
          "Probe commit: " + (.probe_commit // "unknown") + ".\n" +
          "Status: " + (.status // "unknown") + ".\n" +
          "Action: bring this node to >= target commit, deploy ccc-distill-check.sh, " +
          "and run `bash scripts/ccc-distill-check.sh --json` so the fleet matrix can flip " +
          "the node from blocked to verified."
        )
      }
  ]
')"

# Final assembly.
jq -nc \
  --arg issue "82" \
  --arg target "$TARGET_COMMIT" \
  --argjson summary "$SUMMARY" \
  --argjson nodes "[$NODES_JSON]" \
  --argjson subissues "$SUB_JSON" \
  --arg status_file "${STATUS_FILE:-}" \
  --arg probe_file "${PROBE_FILE:-}" \
  '{
     issue: ($issue | tonumber),
     target_commit: $target,
     sources: {
       status_file: (if $status_file == "" then null else $status_file end),
       path_probe_file: (if $probe_file == "" then null else $probe_file end)
     },
     nodes: $nodes,
     summary: $summary,
     recommended_subissues: $subissues
   }'
