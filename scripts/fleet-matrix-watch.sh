#!/usr/bin/env bash
# Read-only Matrix frontend watch. Reports one body-free status per node.
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PROBE="$ROOT/scripts/fleet_matrix_probe.py"
NODES="${CCC_FLEET_NODES:-seoseo dungae sogyo nosuk bangtong yukson soonwook gwakga jingun gongmyoung gongyung daegyo}"
SSH_BIN="${CCC_FLEET_SSH:-ssh}"
SELF="${CCC_FLEET_SELF:-$(hostname -s 2>/dev/null || echo _none_)}"
RETRIES="${CCC_FLEET_RETRIES:-2}"
RETRY_DELAY="${CCC_FLEET_RETRY_DELAY:-10}"
case "$RETRIES" in ''|*[!0-9]*) RETRIES=2 ;; esac
case "$RETRY_DELAY" in ''|*[!0-9]*) RETRY_DELAY=10 ;; esac
[ "$RETRIES" -le 5 ] || RETRIES=5
[ "$RETRY_DELAY" -le 120 ] || RETRY_DELAY=120

fail=0
for node in $NODES; do
  attempt=0
  while :; do
    if [ "$node" = "$SELF" ]; then
      out=$(python3 "$PROBE" 2>/dev/null); rc=$?
    else
      out=$(timeout 30 "$SSH_BIN" -o BatchMode=yes -o ConnectTimeout=8 "$node" python3 - < "$PROBE" 2>/dev/null); rc=$?
    fi
    [ -n "$out" ] && break
    attempt=$((attempt + 1))
    [ "$attempt" -gt "$RETRIES" ] && break
    [ "$RETRY_DELAY" -gt 0 ] && sleep "$RETRY_DELAY"
  done
  if [ -z "$out" ]; then
    echo "UNREACHABLE $node channel=matrix"; fail=1; continue
  fi
  if [ "$rc" != 0 ] || [ "$(printf '%s\n' "$out" | tail -1)" != PROBE_COMPLETE=1 ]; then
    echo "UNVERIFIED $node channel=matrix inspection=incomplete-probe"; fail=1; continue
  fi
  status=$(printf '%s\n' "$out" | sed -n 's/^MATRIX_STATUS=//p' | head -1)
  reason=$(printf '%s\n' "$out" | sed -n 's/^MATRIX_REASON=//p' | head -1)
  case "$status:$reason" in
    OK:available|OK:db-ready|DOWN:no-process|DOWN:health-unavailable|DOWN:db-stopped|DEGRADED:health-degraded|DEGRADED:db-network-retry|UNVERIFIED:multiple-processes|UNVERIFIED:process-inspection|UNVERIFIED:data-directory|UNVERIFIED:health-size|UNVERIFIED:health-unreadable|UNVERIFIED:health-shape|UNVERIFIED:health-pid|UNVERIFIED:health-started|UNVERIFIED:matrix-config|UNVERIFIED:matrix-db|UNVERIFIED:matrix-db-stale|UNVERIFIED:matrix-db-before-process|UNVERIFIED:matrix-db-state) ;;
    *) echo "UNVERIFIED $node channel=matrix inspection=malformed-result"; fail=1; continue ;;
  esac
  echo "$status $node channel=matrix reason=$reason"
  [ "$status" = OK ] || fail=1
done
exit "$fail"
