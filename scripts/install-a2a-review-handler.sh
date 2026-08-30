#!/usr/bin/env bash
# install-a2a-review-handler.sh — install the canonical skills-intake review
# dispatcher + handler from this repo onto a node, replacing (with backup) any
# drifted node-local copies.
#
# Usage:
#   install-a2a-review-handler.sh [--dest DIR]
#
# --dest defaults to /usr/local/sbin (Termux nodes: pass
# $PREFIX/usr/local/sbin or the worker root — anywhere on PATH the
# a2a-hermes-worker env can reach). After installing, wire the worker env
# (e.g. /etc/default/a2a-hermes-worker or the Termux canonical env file):
#
#   WORKER_HANDLER_COMMAND=<dest>/a2a-intent-dispatcher.sh
#   WORKER_HANDLER_ARGS_JSON=[]
#   A2A_WORKER_HANDLER_COMMAND=<dest>/a2a-intent-dispatcher.sh
#   A2A_WORKER_HANDLER_ARGS_JSON=[]
#   # agent/main alignment (optional; default is the claude CLI):
#   REVIEW_AGENT_BIN=/opt/piri/pi-test.sh
#   REVIEW_AGENT_ARGS="-p --no-tools --model xai/grok-4.6"
#
# then restart a2a-hermes-worker.service (or the Termux supervisor) and
# confirm the worker re-registers with the broker.
set -uo pipefail

DEST="/usr/local/sbin"
while [ $# -gt 0 ]; do
  case "$1" in
    --dest) DEST="${2:?--dest requires a directory}"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

HERE="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$DEST"
installed=0
for name in a2a-intent-dispatcher.sh skills-intake-review-handler.sh; do
  src="$HERE/$name"
  [ -f "$src" ] || { echo "missing source: $src" >&2; exit 1; }
  dst="$DEST/$name"
  if [ -f "$dst" ]; then
    before="$(md5sum "$dst" | cut -d' ' -f1)"
    backup="$dst.bak-$(date +%Y%m%dT%H%M%S)"
    cp -p "$dst" "$backup"
    echo "backup: $backup (md5 $before)"
  fi
  install -m 755 "$src" "$dst"
  echo "installed: $dst (md5 $(md5sum "$dst" | cut -d' ' -f1))"
  installed=$((installed + 1))
done
echo "installed $installed file(s) into $DEST"
echo "next: wire WORKER_HANDLER_COMMAND/A2A_WORKER_HANDLER_COMMAND to $DEST/a2a-intent-dispatcher.sh, restart the worker, verify broker re-registration."
