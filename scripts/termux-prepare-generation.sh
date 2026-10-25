#!/usr/bin/env bash
# termux-prepare-generation.sh — build the prepared bridge generation for the
# checkout's HEAD and move the bridge-current pointer to it (#2207 ③).
#
# Why: on Termux the bridge does not run from the checkout. It runs from a
# prepared generation — ~/.ccc-node/preparations/<gen>/{source,job} — that
# the bridge-current symlink points at. A self-update fast-forward therefore
# changes nothing until a generation for the new SHA exists and the pointer
# moves; until then every restart re-launches the OLD code and the tick ends
# `activation incomplete (serving-mismatch)`. Two nodes stayed weeks behind
# that way (2026-10-02) and needed the same manual sequence on 2026-10-09.
# This script is that sequence, made idempotent and fail-closed, intended
# as the self-update `prepare-cmd` (runs after setup.sh, before any restart):
#
#   printf 'exec bash %s/ccc-node/scripts/termux-prepare-generation.sh\n' "$HOME" \
#     > ~/.claude/self-update.prepare-cmd
#
# What it does:
#   1. target = CCC_SELF_UPDATE_TARGET_SHA (set by self-update) or the repo HEAD.
#   2. If the serving generation's source is already at target → exit 0.
#   3. If a ready generation for target exists → repoint only.
#   4. Else create <preparations>/main-<sha7>-<YYYYMMDD>[-<hhmmss>]:
#      source = `git worktree add --detach` of the checkout at target,
#      bridge/.env copied (0600) from the SERVING generation (the effective
#      .env lives in the generation, not the checkout), then
#      bridge/termux_prepare.py builds job/ — reusing the serving generation's
#      native wheels when bridge/requirements.lock.txt is unchanged between the
#      two SHAs (minutes instead of ~1 h; the tool itself re-verifies the
#      toolchain and refuses a stale reuse).
#   5. Receipt must say `ready`, AND — when this node runs the Matrix
#      frontend (~/.ccc-matrix or the runit service exists) — the runtime must
#      import the Matrix extra (`aiohttp`, `nio`): `termux_prepare.py` only
#      installs it with `--extra matrix` (ccc-node#2202), which is passed when
#      the tool supports it. A generation without it is never promoted: the
#      2026-10-09 switch to such a generation crash-looped Matrix on both
#      Termux nodes twice (ccc-node#2175). Then the previous pointer target is
#      recorded in <ccc-node>/bridge-current.prev-<ts> and the symlink moves.
#   The restart is NOT done here — self-update's restart-cmd (restart-frontends)
#   or the operator does it; the pointer is what every launcher reads.
#   6. Prune (#2361/#2362 — each generation is ~1 GB and nothing removed them:
#      23 generations / 12 GB on one node). Runs only after a successful
#      repoint or on the already-serving path, never after a failure. Only
#      script-built `main-*` generations are candidates; KEPT always: the
#      serving generation, the targets of the newest
#      CCC_TERMUX_GENERATIONS_KEEP (2, min 1) bridge-current.prev-* records
#      (the operator's rollback targets), any generation a live process
#      references (cmdline/exe/cwd), and any generation changed in the last
#      24 h (a build in progress or a fresh failure kept for diagnosis).
#      Removal = `git worktree remove --force` + rm -rf + `git worktree prune`.
#      Fail-closed: no resolvable serving generation → nothing is removed.
#      --dry-run reports `would prune`; CCC_TERMUX_PREPARE_PRUNE=0 disables.
#
# Usage: termux-prepare-generation.sh [--dry-run] [--status] [--target <sha>]
# Env:   CCC_SELF_UPDATE_TARGET_SHA, CCC_SELF_UPDATE_REPO_DIR (else
#        ~/.claude/self-update.repo, else ~/ccc-node), CCC_TERMUX_CCC_NODE_DIR
#        (~/.ccc-node), CCC_TERMUX_PYTHON ($PREFIX/bin/python3 — the BASE
#        interpreter; termux_prepare.py refuses a venv python),
#        CCC_TERMUX_PREPARE_TIMEOUT (passed to the tool as --timeout-seconds;
#        see "Prepare timeout" below), CCC_TERMUX_PREPARE_ENV_FILE,
#        CCC_TERMUX_PREPARE_JOBS (1), CCC_TERMUX_PREPARE_LOG
#        (~/.claude/state/termux-prepare.log), CCC_TERMUX_MATRIX_FRONTEND
#        (1/0 — override the Matrix-frontend detection),
#        CCC_TERMUX_GENERATIONS_KEEP (2), CCC_TERMUX_PREPARE_PRUNE (1; 0 = off).
# Prepare timeout (#2487): explicit CCC_TERMUX_PREPARE_TIMEOUT env > the
#        operator-owned per-node file
#        ${CCC_TERMUX_PREPARE_ENV_FILE:-${XDG_CONFIG_HOME:-~/.config}/ccc-node/termux-prepare.env}
#        (0600, `CCC_TERMUX_PREPARE_TIMEOUT=<seconds>`, 1..6900; parsed, never
#        sourced) > 3300 s. ccc-self-update.sh reads the same file and raises
#        its prepare-cmd budget to cover it (+300 s). A bad file (owner, mode,
#        value) is ignored with a warning, never fatal. A slow node needs this
#        when bridge/requirements.lock.txt changes: no wheel reuse, a full
#        native build of ~50+ min (a slow node 2026-10-22..26: native-wheels
#        timeout at 3300 s four ticks in a row). The value and its source
#        (env/file/default) appear in the dry-run, --status and build lines.
# Exit:  0 ok (serving or repointed) · 2 usage/precondition · 3 build failed ·
#        4 receipt not ready or Matrix extra missing · 5 pointer switch failed.
set -euo pipefail

H="${HOME:?}"
CN="${CCC_TERMUX_CCC_NODE_DIR:-$H/.ccc-node}"
PREP="$CN/preparations"
POINTER="$CN/bridge-current"
LOGF="${CCC_TERMUX_PREPARE_LOG:-$H/.claude/state/termux-prepare.log}"
PY="${CCC_TERMUX_PYTHON:-${PREFIX:-/usr}/bin/python3}"
JOBS="${CCC_TERMUX_PREPARE_JOBS:-1}"
DRY=0; STATUS=0; TARGET="${CCC_SELF_UPDATE_TARGET_SHA:-}"
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --status) STATUS=1 ;;
    --target) TARGET="${2:-}"; shift ;;
    -h|--help) sed -n '2,45p' "$0"; exit 0 ;;
    *) echo "termux-prepare-generation: unknown argument $1" >&2; exit 2 ;;
  esac
  shift
done

ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() { mkdir -p "$(dirname "$LOGF")" 2>/dev/null || :; printf '%s %s\n' "$(ts)" "$*" 2>/dev/null >> "$LOGF" || :; }
say() { printf 'termux-prepare-generation: %s\n' "$*"; log "$*"; }
die() { say "$2" >&2; exit "$1"; }

# --- prepare timeout: env > operator file > default (#2487) ------------------
TIMEOUT_DEFAULT=3300
if [ -n "${CCC_TERMUX_PREPARE_TIMEOUT:-}" ]; then
  TIMEOUT_S="$CCC_TERMUX_PREPARE_TIMEOUT"; TIMEOUT_SRC="env"
else
  TIMEOUT_S="$TIMEOUT_DEFAULT"; TIMEOUT_SRC=default
  _paths_lib="$(dirname "${BASH_SOURCE[0]:-$0}")/lib/harness-paths.sh"
  # shellcheck source=lib/harness-paths.sh
  if [ -r "$_paths_lib" ] && . "$_paths_lib" && declare -F ccc_termux_prepare_read_env_file >/dev/null; then
    ccc_termux_prepare_read_env_file
    for _w in "${CCC_TERMUX_PREPARE_FILE_WARNINGS[@]}"; do say "warning: $_w (timeout falls back to ${TIMEOUT_DEFAULT}s)"; done
    if [ -n "$CCC_TERMUX_PREPARE_FILE_TIMEOUT" ]; then TIMEOUT_S="$CCC_TERMUX_PREPARE_FILE_TIMEOUT"; TIMEOUT_SRC="file"; fi
  else
    say "warning: $_paths_lib unavailable; operator timeout file not read (timeout ${TIMEOUT_DEFAULT}s)"
  fi
  unset _paths_lib _w
fi

resolve_repo() {
  if [ -n "${CCC_SELF_UPDATE_REPO_DIR:-}" ]; then printf '%s' "$CCC_SELF_UPDATE_REPO_DIR"; return; fi
  local f="$H/.claude/self-update.repo" line
  if [ -f "$f" ]; then
    while IFS= read -r line; do
      line="${line%%#*}"; line="$(printf '%s' "$line" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
      [ -n "$line" ] && { printf '%s' "$line"; return; }
    done < "$f"
  fi
  printf '%s' "$H/ccc-node"
}

receipt_status() { # <job-dir> — "ready" / other / "" when unreadable
  [ -f "$1/receipt.json" ] || { printf ''; return; }
  "$PY" - "$1/receipt.json" <<'PYEOF' 2>/dev/null || printf ''
import json, sys
try:
    print(str(json.load(open(sys.argv[1])).get("status", "")))
except Exception:
    print("")
PYEOF
}

gen_head() { git -C "$1/source" rev-parse HEAD 2>/dev/null || printf ''; }

prepare_path() { # PATH for termux_prepare.py: no venv bin, no generation runtime bin
  if [ -n "${CCC_TERMUX_PREPARE_PATH:-}" ]; then printf '%s' "$CCC_TERMUX_PREPARE_PATH"; return; fi
  local out="" p
  local IFS=:
  for p in $PATH; do
    [ -n "${VIRTUAL_ENV:-}" ] && [ "$p" = "$VIRTUAL_ENV/bin" ] && continue
    case "$p" in "$PREP"/*) continue ;; esac
    out="${out:+$out:}$p"
  done
  printf '%s' "$out"
}

matrix_frontend_configured() {
  case "${CCC_TERMUX_MATRIX_FRONTEND:-}" in 1) return 0 ;; 0) return 1 ;; esac
  [ -d "$H/.ccc-matrix" ] || [ -e "${PREFIX:-/usr}/var/service/ccc-matrix-bridge" ]
}

# The Matrix frontend imports aiohttp/matrix-nio at startup (transport.py); a
# runtime without them crash-loops within seconds while the Telegram bridge
# looks perfectly healthy. Gate every promotion on a real import.
matrix_extra_present() { # <gen-dir>
  local rpy="$1/job/runtime/bin/python"
  [ -x "$rpy" ] || return 1
  "$rpy" -c "import aiohttp, nio" >/dev/null 2>&1
}

gate_generation() { # <gen-dir> — refuse to promote a generation this node's frontends cannot run
  if matrix_frontend_configured && ! matrix_extra_present "$1"; then
    die 4 "generation $(basename "$1") lacks the Matrix extra (aiohttp/matrix-nio) and this node runs the Matrix frontend — not promoting (needs termux_prepare.py --extra matrix, ccc-node#2202); pointer unchanged"
  fi
}

# --- prune old generations (#2362) -------------------------------------------
KEEP_PREV="${CCC_TERMUX_GENERATIONS_KEEP:-2}"
case "$KEEP_PREV" in ''|*[!0-9]*) KEEP_PREV=2 ;; esac
[ "$KEEP_PREV" -ge 1 ] || KEEP_PREV=1   # the last rollback target always survives

live_process_paths() { # cmdline/exe/cwd of every visible process, one per line
  local p
  for p in /proc/[0-9]*; do
    { tr '\0' ' ' < "$p/cmdline"; printf ' %s %s\n' "$(readlink "$p/exe")" "$(readlink "$p/cwd")"; } 2>/dev/null || :
  done
}

prune_generations() { # best-effort: never changes the exit status of the run
  case "${CCC_TERMUX_PREPARE_PRUNE:-1}" in 0|off|false|no) say "prune: disabled (CCC_TERMUX_PREPARE_PRUNE)"; return 0 ;; esac
  [ -d "$PREP" ] || return 0
  local serving rec t d name live removed=0
  serving="$(readlink -e "$POINTER" 2>/dev/null || true)"
  if [ -z "$serving" ] || [ ! -d "$serving" ]; then
    say "prune: skipped — serving generation unresolved (fail-closed)"; return 0
  fi
  local -A keep=()
  keep["$serving"]=1
  while IFS= read -r rec; do
    [ -n "$rec" ] || continue
    t="$(head -n 1 "$rec" 2>/dev/null || true)"
    [ -n "$t" ] || continue
    case "$t" in /*) ;; *) t="$CN/$t" ;; esac
    t="$(readlink -e "$t" 2>/dev/null || true)"
    [ -n "$t" ] && keep["$t"]=1
  done < <(compgen -G "$CN/bridge-current.prev-*" | sort -r | head -n "$KEEP_PREV")
  live="$(live_process_paths)"
  for d in "$PREP"/main-*; do
    [ -d "$d" ] && [ ! -L "$d" ] || continue
    name="$(basename "$d")"
    d="$(readlink -e "$d" 2>/dev/null || true)"
    [ -n "$d" ] || continue
    [ -n "${keep[$d]:-}" ] && continue
    if grep -qF -e "$PREP/$name/" -e "$PREP/$name " -e "$d/" -e "$d " <<<"$live"; then
      say "prune: keep $name (referenced by a live process)"; continue
    fi
    if [ -n "$(find "$d" -maxdepth 2 -mmin -1440 2>/dev/null | head -n 1)" ]; then
      continue
    fi
    if [ "$DRY" = 1 ]; then say "dry-run: would prune $name"; continue; fi
    if [ -e "$d/source/.git" ]; then
      git -C "$REPO" worktree remove --force --force "$d/source" >/dev/null 2>&1 || :
    fi
    if rm -rf -- "$d" 2>/dev/null && [ ! -e "$d" ]; then
      removed=$((removed + 1)); say "prune: removed $name"
    else
      say "prune: could not remove $name"
    fi
  done
  [ "$DRY" = 1 ] || git -C "$REPO" worktree prune >/dev/null 2>&1 || :
  [ "$DRY" = 1 ] || say "prune: removed $removed generation(s); kept serving + ${KEEP_PREV} rollback target(s), live and <24h generations"
  return 0
}

REPO="$(resolve_repo)"
[ -d "$REPO/.git" ] || [ -f "$REPO/.git" ] || die 2 "checkout not found: $REPO"
[ -n "$TARGET" ] || TARGET="$(git -C "$REPO" rev-parse HEAD)"
[[ "$TARGET" =~ ^[0-9a-f]{40}$ ]] || die 2 "target is not a full 40-hex commit: $TARGET"
git -C "$REPO" cat-file -e "$TARGET^{commit}" 2>/dev/null || die 2 "target $TARGET is not in $REPO"
SHORT="${TARGET:0:7}"

SERVING=""; SERVING_HEAD=""
if [ -L "$POINTER" ] || [ -e "$POINTER" ]; then
  SERVING="$(readlink -e "$POINTER" 2>/dev/null || true)"
  [ -n "$SERVING" ] && SERVING_HEAD="$(gen_head "$SERVING")"
fi

if [ "$STATUS" = 1 ]; then
  say "repo=$REPO head=$(git -C "$REPO" rev-parse --short HEAD) target=$SHORT serving=${SERVING:-none} serving_head=${SERVING_HEAD:0:7} timeout=${TIMEOUT_S}s source=$TIMEOUT_SRC"
  exit 0
fi

if [ -n "$SERVING_HEAD" ] && [ "$SERVING_HEAD" = "$TARGET" ]; then
  say "already serving $SHORT ($(basename "$SERVING")); nothing to do"
  prune_generations
  exit 0
fi

# A ready generation for the target may already exist (an earlier tick built
# it but the pointer switch or restart did not happen): repoint, do not rebuild.
GEN=""
for d in "$PREP"/main-"$SHORT"-*/; do
  [ -d "$d" ] || continue
  d="${d%/}"
  if [ "$(gen_head "$d")" = "$TARGET" ] && [ "$(receipt_status "$d/job")" = "ready" ]; then
    GEN="$d"; break
  fi
done

switch_pointer() { # <gen-dir>
  local gen="$1" prev
  prev="$(readlink "$POINTER" 2>/dev/null || true)"
  if [ -n "$prev" ]; then
    printf '%s\n' "$prev" > "$CN/bridge-current.prev-$(date +%Y%m%dT%H%M%S)" || die 5 "could not record the previous pointer"
  fi
  ln -sfn "preparations/$(basename "$gen")" "$POINTER" || die 5 "could not repoint $POINTER"
  [ "$(readlink -e "$POINTER")" = "$(readlink -e "$gen")" ] || die 5 "pointer verification failed"
  say "pointer: ${prev:-none} -> preparations/$(basename "$gen") (restart follows via restart-cmd)"
}

if [ -n "$GEN" ]; then
  [ "$DRY" = 1 ] && { say "dry-run: would repoint to existing ready generation $(basename "$GEN")"; exit 0; }
  gate_generation "$GEN"
  switch_pointer "$GEN"
  prune_generations
  exit 0
fi

# --- build a new generation ---------------------------------------------------
[ -n "$SERVING" ] || die 2 "no serving generation at $POINTER — the first generation is prepared by hand (docs/termux-build-preparation.md)"
[ -f "$SERVING/source/bridge/.env" ] || die 2 "serving generation has no bridge/.env to inherit: $SERVING/source/bridge/.env"
[ -x "$PY" ] || die 2 "python not executable: $PY (set CCC_TERMUX_PYTHON to the base interpreter)"

GEN="$PREP/main-$SHORT-$(date +%Y%m%d)"
[ -e "$GEN" ] && GEN="$GEN-$(date +%H%M%S)"   # a failed earlier attempt is kept for diagnosis
REUSE=()
if [ -n "$SERVING_HEAD" ] && [ "$(receipt_status "$SERVING/job")" = "ready" ] \
   && git -C "$REPO" diff --quiet "$SERVING_HEAD" "$TARGET" -- bridge/requirements.lock.txt 2>/dev/null; then
  REUSE=(--reuse-wheels-from "$SERVING/job")
fi
EXTRA=()
if matrix_frontend_configured; then
  # Pass --extra matrix when the target's termux_prepare.py supports it
  # (ccc-node#2202). Older tools cannot install the extra; the build still
  # runs but the gate below refuses to promote the result.
  if git -C "$REPO" show "$TARGET:bridge/termux_prepare.py" 2>/dev/null | grep -q -- '"--extra"'; then
    EXTRA=(--extra matrix)
  else
    say "note: termux_prepare.py at $SHORT has no --extra support; the build cannot include the Matrix extra and will not be promoted"
  fi
fi
if [ "$DRY" = 1 ]; then
  say "dry-run: would build $(basename "$GEN") from $SERVING_HEAD -> $SHORT reuse=${REUSE[*]:-none} extra=${EXTRA[*]:-none} timeout=${TIMEOUT_S}s source=$TIMEOUT_SRC"
  exit 0
fi

umask 077
mkdir -p "$PREP" "$GEN"
chmod 700 "$PREP" "$GEN"
say "building $(basename "$GEN") (serving $(basename "$SERVING") @${SERVING_HEAD:0:7}; reuse=${REUSE[*]:-none}; extra=${EXTRA[*]:-none}; timeout=${TIMEOUT_S}s source=$TIMEOUT_SRC)"
git -C "$REPO" worktree add --detach "$GEN/source" "$TARGET" >>"$GEN/prepare.log" 2>&1 \
  || die 3 "git worktree add failed (see $GEN/prepare.log)"
install -m 600 "$SERVING/source/bridge/.env" "$GEN/source/bridge/.env" || die 3 "could not inherit bridge/.env"
started=$SECONDS
# The tool must see the BASE interpreter's environment: a shell spawned by a
# running bridge carries VIRTUAL_ENV / a generation runtime first in PATH, and
# with those the backend probe failed (2026-10-08). Drop the venv variables and
# any venv/generation bin entries from PATH (CCC_TERMUX_PREPARE_PATH overrides).
if ! env -u VIRTUAL_ENV -u PYTHONPATH -u PYTHONHOME -u ANDROID_API_LEVEL PATH="$(prepare_path)" \
     "$PY" -B "$GEN/source/bridge/termux_prepare.py" --work-dir "$GEN/job" --verify-reinstall \
     --timeout-seconds "$TIMEOUT_S" --jobs "$JOBS" "${REUSE[@]}" "${EXTRA[@]}" >>"$GEN/prepare.log" 2>&1; then
  die 3 "termux_prepare.py failed after $((SECONDS - started))s (log: $GEN/prepare.log); pointer unchanged"
fi
status="$(receipt_status "$GEN/job")"
[ "$status" = "ready" ] || die 4 "receipt status is '${status:-missing}', not ready (log: $GEN/prepare.log); pointer unchanged"
say "built $(basename "$GEN") in $((SECONDS - started))s (receipt ready)"
gate_generation "$GEN"
switch_pointer "$GEN"
prune_generations
