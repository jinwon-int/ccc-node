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
# What it does (nothing is deleted, ever):
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
#   5. Receipt must say `ready`; then the previous pointer target is recorded
#      in <ccc-node>/bridge-current.prev-<ts> and the symlink is repointed.
#   The restart is NOT done here — self-update's restart-cmd (restart-frontends)
#   or the operator does it; the pointer is what every launcher reads.
#
# Usage: termux-prepare-generation.sh [--dry-run] [--status] [--target <sha>]
# Env:   CCC_SELF_UPDATE_TARGET_SHA, CCC_SELF_UPDATE_REPO_DIR (else
#        ~/.claude/self-update.repo, else ~/ccc-node), CCC_TERMUX_CCC_NODE_DIR
#        (~/.ccc-node), CCC_TERMUX_PYTHON ($PREFIX/bin/python3 — the BASE
#        interpreter; termux_prepare.py refuses a venv python),
#        CCC_TERMUX_PREPARE_TIMEOUT (3300 s, passed to the tool),
#        CCC_TERMUX_PREPARE_JOBS (1), CCC_TERMUX_PREPARE_LOG
#        (~/.claude/state/termux-prepare.log).
# Exit:  0 ok (serving or repointed) · 2 usage/precondition · 3 build failed ·
#        4 receipt not ready · 5 pointer switch failed.
set -euo pipefail

H="${HOME:?}"
CN="${CCC_TERMUX_CCC_NODE_DIR:-$H/.ccc-node}"
PREP="$CN/preparations"
POINTER="$CN/bridge-current"
LOGF="${CCC_TERMUX_PREPARE_LOG:-$H/.claude/state/termux-prepare.log}"
PY="${CCC_TERMUX_PYTHON:-${PREFIX:-/usr}/bin/python3}"
TIMEOUT_S="${CCC_TERMUX_PREPARE_TIMEOUT:-3300}"
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
  say "repo=$REPO head=$(git -C "$REPO" rev-parse --short HEAD) target=$SHORT serving=${SERVING:-none} serving_head=${SERVING_HEAD:0:7}"
  exit 0
fi

if [ -n "$SERVING_HEAD" ] && [ "$SERVING_HEAD" = "$TARGET" ]; then
  say "already serving $SHORT ($(basename "$SERVING")); nothing to do"
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
  switch_pointer "$GEN"
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
if [ "$DRY" = 1 ]; then
  say "dry-run: would build $(basename "$GEN") from $SERVING_HEAD -> $SHORT reuse=${REUSE[*]:-none} timeout=${TIMEOUT_S}s"
  exit 0
fi

umask 077
mkdir -p "$PREP" "$GEN"
chmod 700 "$PREP" "$GEN"
say "building $(basename "$GEN") (serving $(basename "$SERVING") @${SERVING_HEAD:0:7}; reuse=${REUSE[*]:-none})"
git -C "$REPO" worktree add --detach "$GEN/source" "$TARGET" >>"$GEN/prepare.log" 2>&1 \
  || die 3 "git worktree add failed (see $GEN/prepare.log)"
install -m 600 "$SERVING/source/bridge/.env" "$GEN/source/bridge/.env" || die 3 "could not inherit bridge/.env"
started=$SECONDS
if ! "$PY" -B "$GEN/source/bridge/termux_prepare.py" --work-dir "$GEN/job" --verify-reinstall \
     --timeout-seconds "$TIMEOUT_S" --jobs "$JOBS" "${REUSE[@]}" >>"$GEN/prepare.log" 2>&1; then
  die 3 "termux_prepare.py failed after $((SECONDS - started))s (log: $GEN/prepare.log); pointer unchanged"
fi
status="$(receipt_status "$GEN/job")"
[ "$status" = "ready" ] || die 4 "receipt status is '${status:-missing}', not ready (log: $GEN/prepare.log); pointer unchanged"
say "built $(basename "$GEN") in $((SECONDS - started))s (receipt ready)"
switch_pointer "$GEN"
