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
# Per-attempt limit for the whole remote probe (#2673): connect, key exchange,
# auth, sending the probe and its answer. 10s was too tight for a far watcher:
# on 2026-11-06 the T2 watcher (RTT ~340 ms) needed 4.1s for `ssh true` and
# 7.5-12.2s for this probe while the node was healthy, so the watch flapped
# UNREACHABLE for 42-45 min four times in two days. Default 20s, cap 30s (the
# main sweep's per-node budget). Worst case at the default: 12 nodes ×
# (2 × 20s + 2s) = 504s, plus the Matrix watch's 264s = 768s, inside the
# 840s fleet-watch-15m task timeout; the main probes then end
# `inspection=deadline` (UNVERIFIED, never silently OK).
TIMEOUT="${CCC_FLEET_HANDE_TIMEOUT:-20}"
case "$TIMEOUT" in ''|*[!0-9]*) TIMEOUT=20 ;; esac
[ "$TIMEOUT" -ge 1 ] || TIMEOUT=20
[ "$TIMEOUT" -le 30 ] || TIMEOUT=30
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
      out=$(timeout "$TIMEOUT" "$SSH_BIN" -o BatchMode=yes -o ConnectTimeout=8 "$node" python3 - < "$PROBE" 2>/dev/null); rc=$?
    fi
    [ -n "$out" ] && break
    attempt=$((attempt + 1))
    [ "$attempt" -gt "$RETRIES" ] && break
    [ "$RETRY_DELAY" -gt 0 ] && sleep "$RETRY_DELAY"
  done
  # An empty answer is classified by the last attempt's exit (#2673): only an
  # ssh transport failure (255) means the node was not reached. A timeout (124)
  # or a probe that died (stderr is discarded) is a failed inspection of a node
  # that did answer the connection — UNVERIFIED, which fleet_watch_state.py
  # confirms over more runs than UNREACHABLE because it is weaker evidence.
  if [ -z "$out" ]; then
    if [ "$node" != "$SELF" ] && [ "$rc" = 255 ]; then
      echo "UNREACHABLE $node channel=hande"
    elif [ "$node" != "$SELF" ] && [ "$rc" = 124 ]; then
      echo "UNVERIFIED $node channel=hande inspection=probe-timeout"
    else
      echo "UNVERIFIED $node channel=hande inspection=probe-error rc=$rc"
    fi
    fail=1; continue
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
