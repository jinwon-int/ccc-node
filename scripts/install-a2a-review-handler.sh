#!/usr/bin/env bash
# install-a2a-review-handler.sh — install the canonical skills-intake review
# dispatcher + review/revise handlers from this repo onto a node, replacing
# (with backup) any drifted node-local copies.
#
# Usage:
#   install-a2a-review-handler.sh [--dest DIR] [--termux]
#   install-a2a-review-handler.sh --list   # print the installed file names
#
# --list is the single source of the handler set: ccc-self-update.sh reads it
# to refresh drifted installed copies (#2357), and ccc-doctor's a2a-handlers
# check is tested against it.
#
# --dest defaults to /usr/local/sbin. --termux switches the Termux profile:
# dest defaults to $HOME/a2a-broker-worker and the env-wiring hints target the
# Termux canonical env file. After installing, wire the worker env
# (e.g. /etc/default/a2a-hermes-worker, or the Termux canonical env file):
#
#   WORKER_HANDLER_COMMAND=<dest>/a2a-intent-dispatcher.sh
#   WORKER_HANDLER_ARGS_JSON=[]
#   A2A_WORKER_HANDLER_COMMAND=<dest>/a2a-intent-dispatcher.sh
#   A2A_WORKER_HANDLER_ARGS_JSON=[]
#   # agent/main alignment (optional; default is the claude CLI):
#   REVIEW_AGENT_BIN=/opt/piri/pi-test.sh
#   REVIEW_AGENT_ARGS="-p --no-tools --model xai/grok-4.6"
#   # danso reviewer (#1665; the wrapper maps the stdin prompt contract onto
#   # danso's --system-context-file, so no danso-side change is needed):
#   REVIEW_AGENT_BIN=<dest>/danso-review-agent.sh
#   REVIEW_AGENT_ARGS="--model glm-5.3-flash"
#
# A restart is needed only when the env wiring above is added or changed: the
# worker reads its env once at startup, but spawns WORKER_HANDLER_COMMAND as a
# fresh child process per task, so replacing the installed files takes effect
# on the next task without a restart (#2357). After changing the env, restart
# a2a-hermes-worker.service (or the Termux supervisor) and confirm the worker
# re-registers with the broker.
set -uo pipefail

HANDLER_FILES=(a2a-intent-dispatcher.sh danso-review-agent.sh skills-intake-review-handler.sh skills-intake-revise-handler.sh)

DEST=""
TERMUX=0
if [ -d /data/data/com.termux/files/usr ] && [ -n "${HOME:-}" ] && [ "$HOME" = "/data/data/com.termux/files/home" ]; then
  TERMUX=1  # auto-detect: running inside Termux
fi
while [ $# -gt 0 ]; do
  case "$1" in
    --dest) DEST="${2:?--dest requires a directory}"; shift 2 ;;
    --termux) TERMUX=1; shift ;;
    --list) printf '%s\n' "${HANDLER_FILES[@]}"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
if [ -z "$DEST" ]; then
  if [ "$TERMUX" = 1 ]; then
    DEST="${HOME}/a2a-broker-worker"
  else
    DEST="/usr/local/sbin"
  fi
fi
mkdir -p "$DEST" || { echo "cannot create dest: $DEST" >&2; exit 1; }

HERE="$(cd "$(dirname "$0")" && pwd)"
installed=0
for name in "${HANDLER_FILES[@]}"; do
  src="$HERE/$name"
  [ -f "$src" ] || { echo "missing source: $src" >&2; exit 1; }
  dst="$DEST/$name"
  if [ -f "$dst" ]; then
    before="$(md5sum "$dst" | cut -d' ' -f1)"
    backup="$dst.bak-$(date +%Y%m%dT%H%M%S)"
    # Never overwrite a copy we could not back up, and never report a failed
    # install as installed: ccc-self-update.sh relies on a non-zero exit.
    cp -p "$dst" "$backup" || { echo "backup failed: $backup" >&2; exit 1; }
    echo "backup: $backup (md5 $before)"
  fi
  install -m 755 "$src" "$dst" || { echo "install failed: $dst" >&2; exit 1; }
  echo "installed: $dst (md5 $(md5sum "$dst" | cut -d' ' -f1))"
  installed=$((installed + 1))
done
echo "installed $installed file(s) into $DEST"
if [ "$TERMUX" = 1 ]; then
  cat <<'TIP'
next (Termux): append to the node canonical env file (~/.a2a/<node>-canonical.env):
  WORKER_HANDLER_COMMAND=$DEST/a2a-intent-dispatcher.sh
  WORKER_HANDLER_ARGS_JSON=[]
  A2A_WORKER_HANDLER_COMMAND=$DEST/a2a-intent-dispatcher.sh
  A2A_WORKER_HANDLER_ARGS_JSON=[]
(substitute $DEST with the real path). Only if you added or changed those
env lines, restart the worker (kill the worker.js pid; the supervisor respawns
with the re-read env) and verify broker re-registration. Handlers are spawned
per task, so replacing the files alone needs no restart. Values containing
spaces must be double-quoted — the env harness rejects unquoted whitespace.
TIP
else
  echo "next: if not wired yet, point WORKER_HANDLER_COMMAND/A2A_WORKER_HANDLER_COMMAND at $DEST/a2a-intent-dispatcher.sh, then restart the worker and verify broker re-registration. Already wired: no restart needed — handlers are spawned per task, so the new files take effect on the next task."
fi
