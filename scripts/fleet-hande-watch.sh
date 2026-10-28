#!/usr/bin/env bash
# Read-only 「한데」 frontend watch (#2569). Reports one body-free status per node.
#
# One node's hande bridge was down for 14.7 h (2026-10-27/28) while this sweep
# watched only Telegram and Matrix, and every hande node dropped all
# unsolicited notices for days (notice_frames off). The probe names both, plus
# a missing native bot or a bot that is not connected to the bridge.
# RETIRED not-installed (no hande frontend on the node) is not a failure, and
# fleet_watch_state.py treats it as normal like the Telegram RETIRED marker.
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PROBE="$ROOT/scripts/fleet_hande_probe.py"
# No built-in node list (fleet node names may not spread, #1451): fleet-bridge-watch
# passes its own; a standalone run names the nodes explicitly.
NODES="${CCC_FLEET_NODES:-}"
if [ -z "$NODES" ]; then
  echo "fleet-hande-watch: CCC_FLEET_NODES is required (space-separated node list)" >&2
  exit 2
fi
SSH_BIN="${CCC_FLEET_SSH:-ssh}"
SELF="${CCC_FLEET_SELF:-$(hostname -s 2>/dev/null || echo _none_)}"
# Same bound as the Matrix watch: 12 nodes × (2 × 10s + 2s) = 264s worst case.
RETRIES="${CCC_FLEET_HANDE_RETRIES:-1}"
RETRY_DELAY="${CCC_FLEET_HANDE_RETRY_DELAY:-2}"
case "$RETRIES" in ''|*[!0-9]*) RETRIES=1 ;; esac
case "$RETRY_DELAY" in ''|*[!0-9]*) RETRY_DELAY=2 ;; esac
[ "$RETRIES" -le 1 ] || RETRIES=1
[ "$RETRY_DELAY" -le 2 ] || RETRY_DELAY=2

fail=0
for node in $NODES; do
  attempt=0
  while :; do
    if [ "$node" = "$SELF" ]; then
      out=$(python3 "$PROBE" 2>/dev/null); rc=$?
    else
      out=$(timeout 10 "$SSH_BIN" -o BatchMode=yes -o ConnectTimeout=8 "$node" python3 - < "$PROBE" 2>/dev/null); rc=$?
    fi
    [ -n "$out" ] && break
    attempt=$((attempt + 1))
    [ "$attempt" -gt "$RETRIES" ] && break
    [ "$RETRY_DELAY" -gt 0 ] && sleep "$RETRY_DELAY"
  done
  if [ -z "$out" ]; then
    echo "UNREACHABLE $node channel=hande"; fail=1; continue
  fi
  if [ "$rc" != 0 ] || [ "$(printf '%s\n' "$out" | tail -1)" != PROBE_COMPLETE=1 ]; then
    echo "UNVERIFIED $node channel=hande inspection=incomplete-probe"; fail=1; continue
  fi
  status=$(printf '%s\n' "$out" | sed -n 's/^HANDE_STATUS=//p' | head -1)
  reason=$(printf '%s\n' "$out" | sed -n 's/^HANDE_REASON=//p' | head -1)
  case "$status:$reason" in
    OK:available|OK:degraded-stale-error|RETIRED:not-installed|DOWN:no-process|DOWN:bot-no-process|DOWN:health-unavailable|DOWN:crash-loop|DEGRADED:health-degraded|DEGRADED:bot-disconnected|DEGRADED:transport-error|DEGRADED:notices-off|UNVERIFIED:multiple-processes|UNVERIFIED:process-inspection|UNVERIFIED:data-directory|UNVERIFIED:health-unreadable|UNVERIFIED:health-shape|UNVERIFIED:health-pid|UNVERIFIED:health-pid-restarting|UNVERIFIED:health-stale|UNVERIFIED:hande-config) ;;
    *) echo "UNVERIFIED $node channel=hande inspection=malformed-result"; fail=1; continue ;;
  esac
  echo "$status $node channel=hande reason=$reason"
  case "$status" in OK|RETIRED) ;; *) fail=1 ;; esac
done
exit "$fail"
