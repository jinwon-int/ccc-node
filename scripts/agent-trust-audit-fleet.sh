#!/usr/bin/env bash
# agent-trust-audit-fleet — periodic fleet agent trust-surface check (ccc-node#2538).
#
# Pipes claude/hooks/lib/agent_trust.py to every node over ssh
# (`python3 - --json`, or locally for CCC_FLEET_SELF), so the check does not
# depend on the age of the remote checkout. Each node's JSON is stored under
# the state dir and its `signature` (endpoint hosts, MCP server names/targets,
# home-level instruction files, warn/danger findings) is compared with the
# node's ACCEPTED BASELINE.
#
# Verdicts per node:
#   DANGER <items>   IOC-class finding (planted AGENTS.md/CLAUDE.md in a download
#                    dir or a shared ancestor root such as /tmp, known-bad npm).
#                    Always fails, even when present in the baseline.
#   NEW <items>      something appeared since the baseline (e.g. a new MCP
#                    server or endpoint override) — fails.
#   GONE <items>     something disappeared — informational.
#   OK | UNBASELINED | UNREACHABLE (fails) | ERROR (collector crashed; fails)
# Exit 1 on DANGER/NEW/UNREACHABLE/ERROR so an agent-cron
# `telegram-owner-on-failure` payload notifies the owner; else 0.
#
# Layout (default $CCC_STATE_DIR/agent-trust-audit):
#   current/<node>.json   latest collection
#   baseline/<node>.json  accepted baseline
#   runs/<utc-ts>.txt     one verdict line per node per run (bounded)
#
# Read-only fleet-wide except the state dir on the hub. The collector prints
# hosts, paths and hashes only — never file contents or secret values.
# Detection only: TM-1306 keeps blocking guards out of the harness.
set -uo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COLLECTOR="${CCC_AGENT_TRUST_COLLECTOR:-$SELF_DIR/../claude/hooks/lib/agent_trust.py}"
NODES="${CCC_FLEET_NODES:-seoseo dungae sogyo nosuk bangtong yukson soonwook gwakga jingun gongmyoung gongyung daegyo}"
SSH_BIN="${CCC_FLEET_SSH:-ssh}"
SELF="${CCC_FLEET_SELF:-$(hostname -s 2>/dev/null || echo _none_)}"
STATE_DIR="${CCC_STATE_DIR:-${HOME:-/root}/.claude/state}"
AUDIT_DIR="${CCC_AGENT_TRUST_AUDIT_DIR:-$STATE_DIR/agent-trust-audit}"
SSH_TIMEOUT="${CCC_FLEET_SSH_TIMEOUT:-90}"
KEEP_RUNS="${CCC_AGENT_TRUST_AUDIT_KEEP_RUNS:-30}"

ACCEPT=0
ACCEPT_NODES=""
QUIET=0
for arg in "$@"; do
  case "$arg" in
    --accept-baseline) ACCEPT=1 ;;
    --accept-baseline=*) ACCEPT=1; ACCEPT_NODES="${arg#--accept-baseline=}" ;;
    --quiet) QUIET=1 ;;
    -h|--help)
      cat <<'EOF'
Usage: agent-trust-audit-fleet.sh [--accept-baseline[=node1,node2]] [--quiet]
Collect the agent trust surface (planted AGENTS.md/CLAUDE.md, endpoint
overrides, MCP servers, known-bad npm) from every fleet node and compare it
with each node's accepted baseline. Verdicts: OK | DANGER | NEW | GONE |
UNBASELINED | UNREACHABLE | ERROR. Exit 1 on DANGER/NEW/UNREACHABLE/ERROR.
--accept-baseline freezes the current collection (all nodes or the list);
DANGER findings keep failing even after acceptance.
Env: CCC_FLEET_NODES, CCC_FLEET_SSH, CCC_FLEET_SELF, CCC_STATE_DIR /
CCC_AGENT_TRUST_AUDIT_DIR, CCC_FLEET_SSH_TIMEOUT, CCC_AGENT_TRUST_COLLECTOR.
EOF
      exit 0
      ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

[ -r "$COLLECTOR" ] || { echo "agent-trust-audit-fleet: collector not found: $COLLECTOR" >&2; exit 2; }
command -v jq >/dev/null 2>&1 || { echo "agent-trust-audit-fleet: jq is required" >&2; exit 2; }
mkdir -p "$AUDIT_DIR/current" "$AUDIT_DIR/baseline" "$AUDIT_DIR/runs" 2>/dev/null || {
  echo "agent-trust-audit-fleet: cannot create $AUDIT_DIR" >&2; exit 2; }

collect() { # <node> -> current/<node>.json ; 0 ok, 1 unreachable, 2 collector error
  local node="$1" out="$AUDIT_DIR/current/$1.json" tmp raw
  tmp="$(mktemp "$out.XXXXXX")" || return 1
  if [ "$node" = "$SELF" ]; then
    raw="$(python3 "$COLLECTOR" --json 2>/dev/null)"
  else
    raw="$(timeout "$SSH_TIMEOUT" "$SSH_BIN" -o BatchMode=yes -o ConnectTimeout=10 "$node" "python3 - --json" <"$COLLECTOR" 2>/dev/null)"
  fi
  if ! printf '%s' "$raw" | jq -e '.schema == "ccc.agent-trust.v1"' >/dev/null 2>&1; then
    rm -f "$tmp"; return 1
  fi
  if printf '%s' "$raw" | jq -e 'has("error")' >/dev/null 2>&1; then
    rm -f "$tmp"; return 2
  fi
  printf '%s\n' "$raw" > "$tmp" && mv "$tmp" "$out"
}

signature() { jq -r '.signature[]?' "$1" 2>/dev/null | LC_ALL=C sort -u; }
dangers() { jq -r '.findings[]? | select(.severity == "danger") | "\(.kind) \(.id)"' "$1" 2>/dev/null | paste -sd ';' -; }

run_id="$(date -u +%Y%m%dT%H%M%SZ)-$$"
run_file="$AUDIT_DIR/runs/$run_id.txt"
fail=0
for node in $NODES; do
  cur="$AUDIT_DIR/current/$node.json"
  base="$AUDIT_DIR/baseline/$node.json"
  collect "$node"; rc=$?
  if [ "$rc" = 1 ]; then
    verdict="UNREACHABLE $node"; fail=1
  elif [ "$rc" = 2 ]; then
    verdict="ERROR $node (collector crashed; run python3 agent_trust.py on the node)"; fail=1
  else
    danger="$(dangers "$cur")"
    accept_this=0
    if [ "$ACCEPT" = 1 ]; then
      if [ -z "$ACCEPT_NODES" ]; then accept_this=1
      else case ",$ACCEPT_NODES," in *",$node,"*) accept_this=1 ;; esac; fi
    fi
    if [ "$accept_this" = 1 ]; then
      cp "$cur" "$base"
      verdict="BASELINE-ACCEPTED $node ($(signature "$cur" | wc -l | tr -d ' ') items)"
    elif [ ! -f "$base" ]; then
      verdict="UNBASELINED $node ($(signature "$cur" | wc -l | tr -d ' ') items; review, then --accept-baseline=$node)"
    else
      new="$(comm -13 <(signature "$base") <(signature "$cur") | cut -f2- | paste -sd ';' -)"
      gone="$(comm -23 <(signature "$base") <(signature "$cur") | cut -f2- | paste -sd ';' -)"
      if [ -n "$new" ]; then
        verdict="NEW $node: $new"; fail=1
        [ -n "$gone" ] && verdict="$verdict | GONE: $gone"
      elif [ -n "$gone" ]; then
        verdict="GONE $node: $gone"
      else
        verdict="OK $node"
      fi
    fi
    if [ -n "$danger" ]; then
      verdict="DANGER $node: $danger | $verdict"; fail=1
    fi
  fi
  printf '%s\n' "$verdict" >> "$run_file"
  [ "$QUIET" = 1 ] || printf '%s\n' "$verdict"
done

ls -1t "$AUDIT_DIR/runs" 2>/dev/null | tail -n +"$((KEEP_RUNS + 1))" | while read -r old; do rm -f "$AUDIT_DIR/runs/$old"; done
exit "$fail"
