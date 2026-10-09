#!/usr/bin/env bash
# termux-bridge-generation.sh — prepared bridge generations on Termux (#2175 C).
#
# A Termux node serves the bridge from a *prepared generation*
#   $ROOT/preparations/<gen>/{source,job[,serving]}
# (source = detached git worktree of one commit, job = termux_prepare.py work
# dir holding the runtime venv). Exactly one pointer names the serving one:
#   $ROOT/bridge-current -> preparations/<gen>
# and every lifecycle caller (self-update restart-cmd, Termux:Boot, health
# self-heal, the runit Matrix runner) goes through `launch` / `print-*`, so a
# generation switch is "repoint one symlink + restart".
#
# Subcommands
#   print-source | print-job      (also --print-source/--print-job) resolve the serving generation to REAL paths
#                                 (prepared_runtime.py refuses symlinked ancestors)
#   launch <start.sh args...>     locked, channel-scrubbed start.sh call against
#                                 the serving generation (Telegram frontend);
#                                 bare `--path …` arguments mean launch too
#   status                        pointer + what each frontend's health reports
#   prepare <rev> [opts]          worktree + provider .env link + termux_prepare
#                                 (same-node wheel reuse, falling back to a full
#                                 build) + extras + serving gate. Never restarts.
#   promote <gen> [opts]          gate, idle-wait, atomic pointer swap, Telegram
#                                 --restart with one-shot recovery to the previous
#                                 generation, pointer rollback on failure, then a
#                                 detached idle-gated Matrix follow.
#   matrix-follow [opts]          restart the runit Matrix frontend once it is idle,
#                                 only when it serves a different generation.
#
# Exit codes: 0 ok · 2 usage · 3 layout/pointer problem · 6 gate refused ·
# 75 busy (idle wait expired, nothing changed) · otherwise the failing
# termux_prepare.py / start.sh status (start.sh 7 = candidate failed and the
# previous generation was verified restored; the pointer is rolled back).
#
# Environment (defaults in brackets)
#   CCC_NODE_STATE_DIR [$HOME/.ccc-node]   CCC_NODE_CHECKOUT [$HOME/ccc-node]
#   CCC_BRIDGE_PROJECT_PATH [$HOME]        CCC_TERMUX_BASE_PYTHON [$PREFIX/bin/python3]
#   CCC_BRIDGE_HEALTH_FILE [$PROJECT/.telegram_bot/health.json]
#   CCC_MATRIX_HEALTH_FILE [$HOME/.ccc-matrix/health.json]
#   CCC_MATRIX_SERVICE [ccc-matrix-bridge] CCC_SV [sv]  SVDIR [$PREFIX/var/service]
set -euo pipefail

PREFIX="${PREFIX:-/data/data/com.termux/files/usr}"
ROOT="${CCC_NODE_STATE_DIR:-$HOME/.ccc-node}"
POINTER="$ROOT/bridge-current"
LOCK="$ROOT/bridge-prepared-lifecycle.lock"
PROJECT="${CCC_BRIDGE_PROJECT_PATH:-$HOME}"
CHECKOUT="${CCC_NODE_CHECKOUT:-$HOME/ccc-node}"
BASE_PY="${CCC_TERMUX_BASE_PYTHON:-$PREFIX/bin/python3}"
HEALTH="${CCC_BRIDGE_HEALTH_FILE:-$PROJECT/.telegram_bot/health.json}"
MATRIX_HEALTH="${CCC_MATRIX_HEALTH_FILE:-$HOME/.ccc-matrix/health.json}"
MATRIX_SERVICE="${CCC_MATRIX_SERVICE:-ccc-matrix-bridge}"
SV="${CCC_SV:-sv}"
SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"

die() { local code="$1"; shift; echo "termux-bridge-generation: $*" >&2; exit "$code"; }
note() { echo "termux-bridge-generation: $*" >&2; }

# resolve_gen <gen-dir> → G_DIR, G_SRC, G_JOB (real paths, validated)
resolve_gen() {
  G_DIR="$(readlink -e "$1")" || die 3 "generation $1 missing"
  G_SRC="$(readlink -e "$G_DIR/source")" || die 3 "$G_DIR/source missing"
  if [ -e "$G_DIR/serving" ]; then
    G_JOB="$(readlink -e "$G_DIR/serving")" || die 3 "$G_DIR/serving dangling"
  else
    G_JOB="$(readlink -e "$G_DIR/job")" || die 3 "$G_DIR/job missing"
  fi
  [ -f "$G_SRC/bridge/start.sh" ] || die 3 "no bridge/start.sh in $G_SRC"
  [ -x "$G_JOB/runtime/bin/python" ] || die 3 "no runtime in $G_JOB"
}

resolve_current() {
  [ -L "$POINTER" ] || die 3 "pointer $POINTER missing"
  resolve_gen "$POINTER"
}

# gate <gen-dir>: the read-only prepared-runtime gate with the candidate's own
# interpreter and source; saves the report as <gen>/prepared-gate.json.
gate() {
  resolve_gen "$1"
  local out="$G_DIR/prepared-gate.json" tmp
  tmp="$(mktemp "$G_DIR/.gate.XXXXXX")" || die 3 "cannot write in $G_DIR"
  if "$G_JOB/runtime/bin/python" -I -B "$G_SRC/bridge/prepared_runtime.py" \
       --bridge-dir "$G_SRC/bridge" --prepared-dir "$G_JOB" > "$tmp" 2> "$G_DIR/prepared-gate.err" \
     && json_field "$tmp" status | grep -qx ready; then
    mv -f "$tmp" "$out"
    return 0
  fi
  mv -f "$tmp" "$out"
  return 6
}

# json_field <file> <dotted.path> → value on stdout (empty when absent/invalid)
json_field() {
  "$BASE_PY" -I -c 'import json,sys
try:
    v = json.load(open(sys.argv[1]))
    for k in sys.argv[2].split("."):
        v = v.get(k) if isinstance(v, dict) else None
    print("" if v is None else v)
except Exception:
    print("")' "$1" "$2" 2>/dev/null || true
}

# idle <health-file>: 0 when the frontend reports no active/waiting turn.
# A missing or unreadable file counts as idle (nothing to protect).
idle() {
  [ -r "$1" ] || return 0
  local active waiting state
  active="$(json_field "$1" workload.active_requests)"
  waiting="$(json_field "$1" workload.waiting_for_turn)"
  state="$(json_field "$1" workload.turn_occupancy.state)"
  [ "${active:-0}" = 0 ] && [ "${waiting:-0}" = 0 ] && { [ -z "$state" ] || [ "$state" = idle ]; }
}

wait_idle() { # <health-file> <seconds>
  local until=$(( $(date +%s) + $2 ))
  while ! idle "$1"; do
    [ "$(date +%s)" -ge "$until" ] && return 1
    sleep "${CCC_BRIDGE_IDLE_POLL_SECONDS:-15}"
  done
}

# serving_source <health-file> → real source dir the frontend reports, if any
serving_source() {
  local dir; dir="$(json_field "$1" runtime_generation.source_dir)"
  [ -n "$dir" ] && readlink -e "$dir" 2>/dev/null || true
}

swap_pointer() { # <gen-dir>: atomic, relative when under $ROOT
  local target="$1" tmp="$ROOT/.bridge-current.$$"
  case "$target" in "$ROOT"/*) target="${target#"$ROOT"/}" ;; esac
  ln -sfn "$target" "$tmp"
  mv -T "$tmp" "$POINTER"
}

cmd_launch() {
  resolve_current
  unset ANDROID_API_LEVEL VIRTUAL_ENV PYTHONHOME PYTHONPATH
  # A shell spawned by the Matrix bridge carries CCC_CHANNEL=matrix and the
  # Matrix data dirs; this launcher only serves the Telegram frontend.
  unset CCC_CHANNEL BOT_DATA_DIR LOGS_DIR SESSION_STORE_PATH CCC_BOT_ENV_FILE
  local v
  for v in $(compgen -e | grep '^CCC_MATRIX_' || true); do unset "$v"; done
  exec flock --exclusive --timeout 300 --close "$LOCK" \
    bash "$G_SRC/bridge/start.sh" --prepared-runtime "$G_JOB" "$@"
}

cmd_status() {
  local cur="none"
  [ -L "$POINTER" ] && cur="$(readlink "$POINTER")"
  echo "pointer: $cur"
  if [ -L "$POINTER" ] && (resolve_gen "$POINTER") 2>/dev/null; then
    resolve_gen "$POINTER"
    echo "pointer source: $G_SRC ($(git -C "$G_SRC" rev-parse --short HEAD 2>/dev/null || echo '?'))"
    echo "pointer job: $G_JOB"
  fi
  local name file src
  for name in telegram matrix; do
    file="$HEALTH"; [ "$name" = matrix ] && file="$MATRIX_HEALTH"
    src="$(serving_source "$file")"
    echo "$name serving: ${src:-unknown} head=$(json_field "$file" runtime_generation.source_git.head | cut -c1-12) idle=$(idle "$file" && echo yes || echo no)"
  done
}

# prepare <rev> [--name N] [--reuse auto|none|<job>] [--extra X]... [--no-reinstall]
cmd_prepare() {
  local rev="" name="" reuse="auto" reinstall=1 extras=()
  while [ $# -gt 0 ]; do
    case "$1" in
      --name) name="${2:?}"; shift 2 ;;
      --reuse) reuse="${2:?}"; shift 2 ;;
      --extra) extras+=(--extra "${2:?}"); shift 2 ;;
      --no-reinstall) reinstall=0; shift ;;
      -*) die 2 "prepare: unknown option $1" ;;
      *) [ -z "$rev" ] || die 2 "prepare: one revision"; rev="$1"; shift ;;
    esac
  done
  [ -n "$rev" ] || die 2 "prepare: revision required"
  local sha
  sha="$(git -C "$CHECKOUT" rev-parse --verify --quiet "$rev^{commit}")" || die 2 "prepare: unknown revision $rev"
  [ -n "$name" ] || name="main-${sha:0:7}-$(date +%Y%m%d)"
  case "$name" in */*|.*|"") die 2 "prepare: invalid name $name" ;; esac
  local gen="$ROOT/preparations/$name"
  [ -e "$gen" ] && die 2 "prepare: $gen already exists (generations are never reused)"
  umask 077
  mkdir -p "$ROOT/preparations"
  mkdir -m 700 "$gen"
  git -C "$CHECKOUT" worktree add --detach "$gen/source" "$sha" > "$gen/git-worktree.log" 2>&1 \
    || die 3 "prepare: worktree failed (see $gen/git-worktree.log)"
  [ "$(git -C "$gen/source" rev-parse HEAD)" = "$sha" ] || die 3 "prepare: worktree head mismatch"
  # The provider .env is gitignored and excluded from the source seal; without
  # it start.sh's token fallback refuses the restart before stopping anything.
  if [ -e "$CHECKOUT/bridge/.env" ] && [ ! -e "$gen/source/bridge/.env" ]; then
    ln -s "$CHECKOUT/bridge/.env" "$gen/source/bridge/.env"
  fi

  local prior=""
  case "$reuse" in
    none) ;;
    auto)
      if [ -L "$POINTER" ] && (resolve_current >/dev/null 2>&1); then
        resolve_current
        [ -f "$G_JOB/receipt.json" ] && prior="$G_JOB"
      fi ;;
    *) prior="$(readlink -e "$reuse")" || die 2 "prepare: reuse job $reuse missing" ;;
  esac

  local args=(--timeout-seconds 7200 "${extras[@]}")
  [ "$reinstall" = 1 ] && args+=(--verify-reinstall)
  local job="job" rc=0 reason
  if [ -n "$prior" ]; then
    run_prepare "$gen" "$job" --reuse-wheels-from "$prior" "${args[@]}" || rc=$?
    # stdout carries the report even when no workspace was claimed.
    reason="$(json_field "$gen/prepare-$job.out" reason)"
    if [ "$rc" != 0 ] && [ "$reuse" = auto ] && case "$reason" in reuse_*) true ;; *) false ;; esac; then
      note "prepare: wheel reuse refused ($reason); full build in $gen/job2"
      job="job2"; rc=0
      run_prepare "$gen" "$job" "${args[@]}" || rc=$?
    fi
  else
    run_prepare "$gen" "$job" "${args[@]}" || rc=$?
  fi
  [ "$rc" = 0 ] || die "$rc" "prepare: termux_prepare.py failed (see $gen/$job/receipt.json)"
  [ "$job" = job ] || ln -s "$job" "$gen/serving"
  gate "$gen" || die 6 "prepare: serving gate refused (see $gen/prepared-gate.json)"
  echo "$gen"
}

run_prepare() { # <gen> <job-name> <termux_prepare args...>
  local gen="$1" job="$2"; shift 2
  env -u VIRTUAL_ENV -u PYTHONPATH -u PYTHONHOME -u ANDROID_API_LEVEL \
    PATH="$PREFIX/bin:/system/bin" \
    "$BASE_PY" -B "$gen/source/bridge/termux_prepare.py" --work-dir "$gen/$job" "$@" \
    > "$gen/prepare-$job.out" 2> "$gen/prepare-$job.log"
}

# promote <gen> [--wait-idle S] [--matrix|--no-matrix] [--matrix-wait S] [--dry-run]
cmd_promote() {
  local target="" wait=300 matrix=auto matrix_wait=3600 dry=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --wait-idle) wait="${2:?}"; shift 2 ;;
      --matrix) matrix=1; shift ;;
      --no-matrix) matrix=0; shift ;;
      --matrix-wait) matrix_wait="${2:?}"; shift 2 ;;
      --dry-run) dry=1; shift ;;
      -*) die 2 "promote: unknown option $1" ;;
      *) [ -z "$target" ] || die 2 "promote: one generation"; target="$1"; shift ;;
    esac
  done
  [ -n "$target" ] || die 2 "promote: generation required"
  case "$target" in /*) ;; *) [ -d "$target" ] || target="$ROOT/preparations/$target" ;; esac
  gate "$target" || die 6 "promote: serving gate refused for $target (nothing changed)"
  local new_dir="$G_DIR" new_src="$G_SRC"
  local prev_dir="" prev_src="" prev_job=""
  if [ -L "$POINTER" ] && (resolve_current >/dev/null 2>&1); then
    resolve_current; prev_dir="$G_DIR" prev_src="$G_SRC" prev_job="$G_JOB"
  fi
  if [ "$matrix" = auto ]; then
    matrix=0; [ -d "${SVDIR:-$PREFIX/var/service}/$MATRIX_SERVICE" ] && matrix=1
  fi
  local serving=0
  [ "$prev_dir" = "$new_dir" ] && [ "$(serving_source "$HEALTH")" = "$new_src/bridge" ] && serving=1
  if [ "$dry" = 1 ]; then
    local action="telegram restart"
    if [ "$serving" = 1 ]; then action="telegram already serving (no restart)"
    elif [ -n "$prev_dir" ] && [ "$prev_dir" != "$new_dir" ]; then action="telegram restart with recovery"
    fi
    echo "promote plan: ${prev_dir:-none} -> $new_dir ($action; matrix follow=$matrix; telegram idle=$(idle "$HEALTH" && echo yes || echo no))"
    return 0
  fi
  local rc=0
  if [ "$serving" = 1 ]; then
    note "promote: $new_dir already serving Telegram"
  else
    wait_idle "$HEALTH" "$wait" || die 75 "promote: Telegram still busy after ${wait}s (nothing changed)"
    swap_pointer "$new_dir"
    local recovery=()
    [ -n "$prev_dir" ] && [ "$prev_dir" != "$new_dir" ] \
      && recovery=(--recovery-source "$prev_src/bridge" --recovery-runtime "$prev_job")
    bash "$SELF" launch --path "$PROJECT" --restart -d "${recovery[@]}" || rc=$?
    if [ "$rc" != 0 ]; then
      if [ -n "$prev_dir" ]; then swap_pointer "$prev_dir"; note "promote: restart rc=$rc, pointer rolled back to $prev_dir"; fi
      return "$rc"
    fi
  fi
  if [ "$matrix" = 1 ]; then
    mkdir -p "$ROOT/logs"
    local log; log="$ROOT/logs/matrix-follow-$(date +%Y%m%dT%H%M%S).log"
    # Detached: when promote runs from a Matrix session, restarting the Matrix
    # frontend ends that session; the follower must outlive it.
    setsid nohup bash "$SELF" matrix-follow --wait "$matrix_wait" > "$log" 2>&1 < /dev/null &
    note "promote: Matrix follow detached (log $log)"
  fi
  return 0
}

cmd_matrix_follow() {
  local wait=3600
  while [ $# -gt 0 ]; do
    case "$1" in --wait) wait="${2:?}"; shift 2 ;; *) die 2 "matrix-follow: unknown option $1" ;; esac
  done
  resolve_current
  local want="$G_SRC/bridge" have
  have="$(serving_source "$MATRIX_HEALTH")"
  if [ "$have" = "$want" ]; then echo "matrix already serves $want"; return 0; fi
  wait_idle "$MATRIX_HEALTH" "$wait" || die 75 "matrix-follow: still busy after ${wait}s (serving ${have:-unknown})"
  SVDIR="${SVDIR:-$PREFIX/var/service}" "$SV" restart "$MATRIX_SERVICE"
  local i
  for i in $(seq 1 "${CCC_MATRIX_FOLLOW_CHECKS:-24}"); do
    sleep "${CCC_MATRIX_FOLLOW_POLL_SECONDS:-5}"
    [ "$(serving_source "$MATRIX_HEALTH")" = "$want" ] && { echo "matrix now serves $want"; return 0; }
  done
  die 4 "matrix-follow: restarted but health does not report $want (i=$i)"
}

case "${1:-}" in
  print-source|--print-source) resolve_current; printf '%s\n' "$G_SRC" ;;
  print-job|--print-job) resolve_current; printf '%s\n' "$G_JOB" ;;
  launch) shift; cmd_launch "$@" ;;
  status) cmd_status ;;
  prepare) shift; cmd_prepare "$@" ;;
  promote) shift; cmd_promote "$@" ;;
  matrix-follow) shift; cmd_matrix_follow "$@" ;;
  -h|--help|"") sed -n '2,40p' "$SELF"; [ -n "${1:-}" ] || exit 2 ;;
  # Drop-in for the operator wrapper's old contract (`bridge-current.sh
  # --path … --restart -d`): leading start.sh options mean `launch`.
  --*) cmd_launch "$@" ;;
  *) die 2 "unknown subcommand $1 (see --help)" ;;
esac
