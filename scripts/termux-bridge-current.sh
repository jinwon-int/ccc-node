#!/usr/bin/env bash
# termux-bridge-current.sh — launcher for the SERVING prepared bridge
# generation on Termux (ccc-node#2175; repo canon of the operator-side
# ~/.ccc-node/scripts/bridge-current.sh that the 2026-10-08 incident produced).
#
# A Termux node serves the bridge from a prepared generation
#   ~/.ccc-node/preparations/<gen>/{source,job[,serving]}
# (source = detached git worktree of one commit, job = termux_prepare.py work
# dir holding the runtime venv; an optional `serving` symlink selects another
# job dir). Exactly one pointer names the serving one:
#   ~/.ccc-node/bridge-current -> preparations/<gen>
# Every lifecycle caller (self-update restart-cmd via termux-restart-frontends,
# Termux:Boot, health self-heal, the runit Matrix runner) resolves that pointer
# through this file, so switching generations is "repoint one symlink +
# restart". termux-prepare-generation.sh moves the pointer; this script never
# does.
#
# prepared_runtime.py refuses symlinked ancestors, so the pointer is resolved
# to REAL paths before anything is handed to start.sh.
#
# Usage:
#   termux-bridge-current.sh --print-source | --print-job | --print-generation
#   termux-bridge-current.sh <start.sh args...>     e.g. --path "$HOME" --restart -d
#
# Launch details (Telegram frontend only — the Matrix frontend has its own
# `env -i` runner):
#   * the call is serialised on $CCC_TERMUX_LAUNCH_LOCK with util-linux flock
#     when present, else an atomic mkdir claim (stale holders are reclaimed by
#     pid liveness); waiting longer than CCC_TERMUX_LAUNCH_LOCK_WAIT_SECONDS
#     (300) exits 1 like flock --timeout does;
#   * channel-scoped variables inherited from a shell the Matrix bridge spawned
#     (CCC_CHANNEL=matrix, BOT_DATA_DIR=~/.ccc-matrix, CCC_MATRIX_*, …) are
#     dropped — with them the "Telegram" daemon came up as a second Matrix
#     frontend and crash-looped on the state lock (2026-10-08);
#   * `--channel telegram` is appended when the serving generation's start.sh
#     understands it (#2177) and the caller did not pass --channel; older
#     generations get the scrubbed environment only.
#
# Env:   CCC_TERMUX_CCC_NODE_DIR (~/.ccc-node), CCC_TERMUX_LAUNCH_LOCK
#        (<ccc-node>/bridge-prepared-lifecycle.lock),
#        CCC_TERMUX_LAUNCH_LOCK_WAIT_SECONDS (300), CCC_TERMUX_LAUNCH_FLOCK
#        (auto | 0 = never use flock, take the mkdir path).
# Exit:  start.sh's status · 1 lock wait expired · 2 usage · 3 pointer/layout
#        problem (pointer dangling, source/job/runtime missing).
set -euo pipefail

H="${HOME:?}"
CN="${CCC_TERMUX_CCC_NODE_DIR:-$H/.ccc-node}"
POINTER="$CN/bridge-current"
LOCK="${CCC_TERMUX_LAUNCH_LOCK:-$CN/bridge-prepared-lifecycle.lock}"
LOCK_WAIT="${CCC_TERMUX_LAUNCH_LOCK_WAIT_SECONDS:-300}"

die() { local code="$1"; shift; echo "termux-bridge-current: $*" >&2; exit "$code"; }

case "${1:-}" in
  -h|--help) sed -n '2,46p' "$0"; exit 0 ;;
  "") echo "usage: termux-bridge-current.sh --print-source|--print-job|--print-generation | <start.sh args...>" >&2; exit 2 ;;
esac

GEN="$(readlink -e "$POINTER")" || die 3 "pointer $POINTER missing or dangling"
SOURCE="$(readlink -e "$GEN/source")" || die 3 "$GEN/source missing"
if [ -e "$GEN/serving" ]; then
  JOB="$(readlink -e "$GEN/serving")" || die 3 "$GEN/serving dangling"
else
  JOB="$(readlink -e "$GEN/job")" || die 3 "$GEN/job missing"
fi
[ -f "$SOURCE/bridge/start.sh" ] || die 3 "no start.sh in $SOURCE/bridge"
[ -x "$JOB/runtime/bin/python" ] || die 3 "no runtime in $JOB"

case "$1" in
  --print-source|print-source) printf '%s\n' "$SOURCE"; exit 0 ;;
  --print-job|print-job) printf '%s\n' "$JOB"; exit 0 ;;
  --print-generation|print-generation) printf '%s\n' "$GEN"; exit 0 ;;
esac

# ── Telegram-only launch: scrub what a Matrix-spawned shell leaks ──
unset ANDROID_API_LEVEL VIRTUAL_ENV PYTHONHOME PYTHONPATH
unset CCC_CHANNEL BOT_DATA_DIR LOGS_DIR SESSION_STORE_PATH CCC_BOT_ENV_FILE
for v in $(compgen -e | grep '^CCC_MATRIX_' || true); do unset "$v"; done

CHANNEL=()
has_channel=0
for a in "$@"; do [ "$a" = --channel ] && has_channel=1; done
if [ "$has_channel" = 0 ] && grep -q -- '--channel)' "$SOURCE/bridge/start.sh" 2>/dev/null; then
  CHANNEL=(--channel telegram)
fi

CMD=(bash "$SOURCE/bridge/start.sh" --prepared-runtime "$JOB" "$@" ${CHANNEL[@]+"${CHANNEL[@]}"})

if [ "${CCC_TERMUX_LAUNCH_FLOCK:-auto}" != 0 ] && command -v flock >/dev/null 2>&1; then
  exec flock --exclusive --timeout "$LOCK_WAIT" --close "$LOCK" "${CMD[@]}"
fi

# No util-linux flock (some Termux installs): atomic mkdir claim next to the
# lock file, the holder's pid decides liveness so a dead holder is reclaimed.
CLAIM="$LOCK.d"
t0=$(date +%s)
until mkdir "$CLAIM" 2>/dev/null; do
  holder="$(cat "$CLAIM/pid" 2>/dev/null || true)"
  if [ -n "$holder" ] && ! kill -0 "$holder" 2>/dev/null; then
    rm -rf "$CLAIM"; continue
  fi
  [ $(( $(date +%s) - t0 )) -ge "$LOCK_WAIT" ] && die 1 "launch lock $CLAIM held by pid ${holder:-?} for ${LOCK_WAIT}s"
  sleep 1
done
printf '%s\n' "$$" > "$CLAIM/pid"
trap 'rm -rf "$CLAIM"' EXIT
rc=0
"${CMD[@]}" || rc=$?
exit "$rc"
