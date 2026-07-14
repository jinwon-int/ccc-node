#!/usr/bin/env bash
# ccc-doctor-fleet-matrix.sh — read-only fleet rollup for ccc-doctor output.
#
# Input is a text evidence file with blocks:
#   ===== <node> =====
#   <ccc-doctor output or JSON>
#
# Output is JSON only. No SSH, no service changes, no provider sends, no secret
# reads. This script only classifies already-collected evidence.
set -euo pipefail

usage() {
  sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'
  echo "Usage: bash $0 --evidence FILE [--node-list n1,n2] [--json]" >&2
  exit "${1:-0}"
}

EVIDENCE=""
NODE_LIST="dungae,nosuk,soonwook,gongyung,daegyo"
while [ $# -gt 0 ]; do
  case "$1" in
    --evidence|--status) EVIDENCE="${2:-}"; shift 2 ;;
    --node-list) NODE_LIST="${2:-}"; shift 2 ;;
    --json) shift ;;
    -h|--help) usage 0 ;;
    *) printf 'unknown arg: %s\n' "$1" >&2; usage 2 ;;
  esac
done

[ -n "$EVIDENCE" ] || { echo "--evidence is required" >&2; exit 2; }
[ -f "$EVIDENCE" ] || { echo "evidence not found: $EVIDENCE" >&2; exit 2; }

# The evidence-block parser and classification core are single-sourced in
# scripts/lib/fleet_matrix.py (#451); this wrapper only owns arg handling.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec python3 "$SCRIPT_DIR/lib/fleet_matrix.py" \
  --domain doctor --evidence "$EVIDENCE" --node-list "$NODE_LIST"
