#!/usr/bin/env bash
# tunnel-audit-fleet — periodic fleet exposure-surface check (ccc-node#1366).
#
# Runs scripts/tunnel-audit.sh on every node over ssh (or locally for
# CCC_FLEET_SELF), stores each node's JSON under the state dir, and compares it
# with the node's ACCEPTED BASELINE. Anything that appeared since the baseline
# was accepted — a tunnel-class unit, a cron tunnel line, a non-loopback
# listener, funnel turning on — is reported as NEW; anything that vanished as
# GONE. Exit 1 on NEW or UNREACHABLE so an agent-cron `telegram-owner-on-failure`
# payload notifies the owner; GONE and unchanged nodes exit 0.
#
# The baseline is the machine-checkable twin of the Family Wiki tunnel
# registry ([DOC-3283]): an operator reviews the registry, then runs
# `--accept-baseline` (per node or all) to freeze what is known. Until a node
# has a baseline it is reported as UNBASELINED (exit 0 — nothing to compare
# yet, but visible so it does not stay silent forever).
#
# Layout (default $CCC_STATE_DIR/tunnel-audit):
#   current/<node>.json    latest collection
#   baseline/<node>.json   accepted baseline
#   runs/<utc-ts>.txt      one-line-per-node verdicts of each run (bounded)
#
# Same conventions as fleet-bridge-watch.sh: CCC_FLEET_NODES, CCC_FLEET_SSH,
# CCC_FLEET_SELF, CCC_FLEET_CANONICAL_ROOTS. The per-node collector is located
# at <runtime-root>/scripts/tunnel-audit.sh; the runtime root is the first
# canonical checkout present on the node (fleet-bridge-watch derives it from
# the live bridge process; an exposure audit must also work on a node whose
# bridge is down, so the checkout probe is used instead).
#
# Read-only fleet-wide except the state dir on the hub. Never prints tokens:
# the collector masks them and this script only forwards its JSON.
set -uo pipefail

NODES="${CCC_FLEET_NODES:-seoseo dungae sogyo nosuk bangtong yukson soonwook gwakga jingun gongmyoung gongyung daegyo}"
SSH_BIN="${CCC_FLEET_SSH:-ssh}"
SELF="${CCC_FLEET_SELF:-$(hostname -s 2>/dev/null || echo _none_)}"
CANON_ROOTS="${CCC_FLEET_CANONICAL_ROOTS:-/opt/ccc-node /root/ccc-node /home/*/ccc-node /data/data/com.termux/files/home/ccc-node}"
STATE_DIR="${CCC_STATE_DIR:-${HOME:-/root}/.claude/state}"
AUDIT_DIR="${CCC_TUNNEL_AUDIT_DIR:-$STATE_DIR/tunnel-audit}"
SSH_TIMEOUT="${CCC_FLEET_SSH_TIMEOUT:-40}"
KEEP_RUNS="${CCC_TUNNEL_AUDIT_KEEP_RUNS:-30}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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
Usage: tunnel-audit-fleet.sh [--accept-baseline[=node1,node2]] [--quiet]
Collect tunnel-audit JSON from every fleet node and compare it with each node's
accepted baseline. Verdicts per node: OK | NEW <items> | GONE <items> |
UNBASELINED | UNREACHABLE | NOSCRIPT. Exit 1 when any node is NEW or
UNREACHABLE (owner notification via agent-cron on-failure), else 0.
--accept-baseline freezes the current collection as the baseline (all nodes,
or the comma-separated list). Env: CCC_FLEET_NODES, CCC_FLEET_SSH,
CCC_FLEET_SELF, CCC_STATE_DIR / CCC_TUNNEL_AUDIT_DIR, CCC_FLEET_SSH_TIMEOUT.
EOF
      exit 0
      ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

mkdir -p "$AUDIT_DIR/current" "$AUDIT_DIR/baseline" "$AUDIT_DIR/runs" 2>/dev/null || {
  echo "tunnel-audit-fleet: cannot create $AUDIT_DIR" >&2; exit 2; }

# Remote probe: locate the first canonical checkout that carries the collector
# and run it. Printed as a single JSON document; anything else is a failure.
read -r -d '' PROBE <<'PROBE_EOF' || true
for r in __ROOTS__; do
  for d in $r; do
    if [ -f "$d/scripts/tunnel-audit.sh" ]; then
      exec bash "$d/scripts/tunnel-audit.sh" --json
    fi
  done
done
echo '{"schema":"ccc.tunnel-audit.v1","error":"no-script"}'
PROBE_EOF
PROBE="${PROBE//__ROOTS__/$CANON_ROOTS}"

collect() { # <node> -> writes current/<node>.json ; returns 0 ok, 1 unreachable, 2 noscript
  local node="$1" out="$AUDIT_DIR/current/$1.json" tmp raw
  tmp="$(mktemp "$out.XXXXXX")" || return 1
  if [ "$node" = "$SELF" ]; then
    raw="$(bash -c "$PROBE" 2>/dev/null)"
  else
    raw="$(timeout "$SSH_TIMEOUT" "$SSH_BIN" -o BatchMode=yes -o ConnectTimeout=10 "$node" "bash -s" <<<"$PROBE" 2>/dev/null)"
  fi
  if ! printf '%s' "$raw" | jq -e '.schema == "ccc.tunnel-audit.v1"' >/dev/null 2>&1; then
    rm -f "$tmp"; return 1
  fi
  if printf '%s' "$raw" | jq -e '.error == "no-script"' >/dev/null 2>&1; then
    rm -f "$tmp"; return 2
  fi
  printf '%s\n' "$raw" > "$tmp" && mv "$tmp" "$out"
}

# Comparable identity of what matters: units by name+kind, cron lines, non-loopback
# listeners by local address + process, funnel flag, residue files, and the host
# firewall (ufw status + default incoming policy + allow-rule hash, #1431) — a
# public bind accepted on the strength of default-deny must re-surface as NEW
# when the policy or the rule set changes. Nodes whose JSON predates the
# firewall block contribute no firewall line, so old baselines compare as NEW
# once (re-accept after reviewing).
signature() { # <json> -> sorted "kind\tid" lines
  jq -r '
    ([.units[]? | "unit\t\(.unit) [\(.kind)]"]
     + [.cron[]? | "cron\t\(.source): \(.line | .[0:120])"]
     + [.listeners[]? | select(.bind != "loopback") | "listener\t\(.local) \(.process // "-") [\(.bind)]"]
     + (if (.exposure.funnel_configured // false) then ["funnel\tenabled"] else [] end)
     + [.residue[]? | "residue\t\(.file)"]
     + (if .firewall.ufw? then
          [ .firewall.ufw
            | if .status == "active"
              then "firewall\tufw active default-in=\(.default_incoming) rules=\(.rules_hash | .[0:8]) (\(.rules | length))"
              else "firewall\tufw \(.status)" end ]
        else [] end))
    | .[]' "$1" 2>/dev/null | sort -u
}

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
    verdict="NOSCRIPT $node (checkout lacks scripts/tunnel-audit.sh — harness too old)"
  else
    accept_this=0
    if [ "$ACCEPT" = 1 ]; then
      if [ -z "$ACCEPT_NODES" ]; then accept_this=1
      else case ",$ACCEPT_NODES," in *",$node,"*) accept_this=1 ;; esac; fi
    fi
    if [ "$accept_this" = 1 ]; then
      cp "$cur" "$base"
      verdict="BASELINE-ACCEPTED $node ($(signature "$cur" | wc -l | tr -d ' ') items)"
    elif [ ! -f "$base" ]; then
      verdict="UNBASELINED $node ($(signature "$cur" | wc -l | tr -d ' ') items; run --accept-baseline=$node after reviewing the registry)"
    else
      new="$(comm -13 <(signature "$base") <(signature "$cur") | cut -f2 | paste -sd ';' -)"
      gone="$(comm -23 <(signature "$base") <(signature "$cur") | cut -f2 | paste -sd ';' -)"
      if [ -n "$new" ]; then
        verdict="NEW $node: $new"; fail=1
        [ -n "$gone" ] && verdict="$verdict | GONE: $gone"
      elif [ -n "$gone" ]; then
        verdict="GONE $node: $gone"
      else
        verdict="OK $node"
      fi
    fi
  fi
  printf '%s\n' "$verdict" >> "$run_file"
  [ "$QUIET" = 1 ] || printf '%s\n' "$verdict"
done

# Bounded run history.
ls -1t "$AUDIT_DIR/runs" 2>/dev/null | tail -n +"$((KEEP_RUNS + 1))" | while read -r old; do rm -f "$AUDIT_DIR/runs/$old"; done
exit "$fail"
