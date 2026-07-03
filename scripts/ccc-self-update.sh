#!/usr/bin/env bash
# ccc-self-update — the PRE-APPROVED node maintenance procedure.
#
# Problem this solves: broker/Gateway/worker service control is (correctly)
# operator_approval_gated in guard.sh, but a fleet node is only useful if it
# can pick up ccc-node updates and restart its own services. Instead of
# loosening the gate, this script IS the approval: a fixed, code-reviewed,
# audited procedure (mirroring the ccc-telegram-bridge restart carve-out
# rationale) that an agent may invoke as a whole. The blast radius stays
# operator-controlled because the ONLY services it will ever touch are the
# ones listed in an operator-owned allowlist file that guard.sh blocks agents
# from writing:
#   ~/.claude/self-update.services   (one systemd unit name per line, # comments)
#   ~/.claude/self-update.repo       (optional: absolute repo path override)
#
# Procedure (run):
#   1. take a lock; resolve the repo (env > repo file > script location > ~/ccc-node)
#   2. preconditions: .git present, clean working tree, on the expected branch
#   3. git fetch + merge --ff-only (never rewrites local history)
#   4. if HEAD changed (or --force): run ./setup.sh to redeploy the harness;
#      on setup failure roll back to the old SHA and abort
#   5. restart each allowlisted service and verify it is active again
#   6. append a JSONL audit record and queue an owner Telegram notification
#      (spool only — this script never touches the bot token)
#
# Modes: run [--force] | status
# Env: CCC_SELF_UPDATE_REPO, CCC_SELF_UPDATE_BRANCH (default main),
#      CCC_SELF_UPDATE_SYSTEMCTL (default systemctl; tests inject a fake),
#      CCC_STATE_DIR, CCC_PUSH_SPOOL, CCC_NODE.
# Exit: 0 = up-to-date or updated cleanly; non-zero = aborted (reason logged).
set -uo pipefail

CLAUDE_DIR="${CCC_CLAUDE_DIR:-${HOME:-/root}/.claude}"
STATE_DIR="${CCC_STATE_DIR:-$CLAUDE_DIR/state}"
LOG="$STATE_DIR/self-update.log"
LOCK="$STATE_DIR/self-update.lock"
SPOOL="${CCC_PUSH_SPOOL:-$STATE_DIR/telegram-spool}"
SERVICES_FILE="${CCC_SELF_UPDATE_SERVICES:-$CLAUDE_DIR/self-update.services}"
REPO_FILE="$CLAUDE_DIR/self-update.repo"
BRANCH="${CCC_SELF_UPDATE_BRANCH:-main}"
SYSTEMCTL="${CCC_SELF_UPDATE_SYSTEMCTL:-systemctl}"
mkdir -p "$STATE_DIR" 2>/dev/null

ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() { printf '%s %s\n' "$(ts)" "$*" >> "$LOG" 2>/dev/null; }
say() { printf '%s\n' "$*"; }

resolve_repo() {
  if [ -n "${CCC_SELF_UPDATE_REPO:-}" ]; then printf '%s' "$CCC_SELF_UPDATE_REPO"; return; fi
  if [ -f "$REPO_FILE" ]; then head -1 "$REPO_FILE" | tr -d '[:space:]'; return; fi
  local here
  here="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)"
  case "$here" in
    */scripts) printf '%s' "${here%/scripts}"; return ;;
  esac
  printf '%s' "${HOME:-/root}/ccc-node"
}

notify() { # <text> <dedup-suffix>
  mkdir -p "$SPOOL" 2>/dev/null || return 0
  local node now fname
  node="${CCC_NODE:-$(hostname -s 2>/dev/null || echo node)}"
  now="$(ts)"
  fname="$SPOOL/$(printf '%s' "$now" | tr ':' '-')-SelfUpdate-$$.json"
  jq -nc --arg ts "$now" --arg node "$node" --arg text "$1" --arg d "$2" \
    '{ts:$ts, event:"SelfUpdate", node:$node, text:$text, dedup:("SelfUpdate:"+$d)}' \
    > "$fname" 2>/dev/null || rm -f "$fname" 2>/dev/null
}

audit() { # <result> <old> <new> <changed> <setup_ok> <services-json>
  jq -nc --arg ts "$(ts)" --arg result "$1" --arg old "$2" --arg new "$3" \
    --argjson changed "$4" --argjson setup_ok "$5" --argjson services "$6" \
    '{ts:$ts, result:$result, old:$old, new:$new, changed:$changed, setup_ok:$setup_ok, services:$services}' \
    >> "$LOG" 2>/dev/null
}

MODE="${1:-run}"
FORCE=0
[ "${2:-}" = "--force" ] && FORCE=1

if [ "$MODE" = "status" ]; then
  REPO="$(resolve_repo)"
  say "repo: $REPO (branch $BRANCH)"
  say "head: $(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo '?')"
  say "lock: $([ -d "$LOCK" ] && echo HELD || echo free)"
  say "services file: $SERVICES_FILE $([ -f "$SERVICES_FILE" ] && echo "($(grep -cv '^[[:space:]]*\(#\|$\)' "$SERVICES_FILE" 2>/dev/null || echo 0) services)" || echo '(missing)')"
  say "-- log (last 5) --"
  tail -5 "$LOG" 2>/dev/null
  exit 0
fi

if [ "$MODE" != "run" ]; then
  say "usage: ccc-self-update.sh [run [--force]|status]" >&2
  exit 2
fi

# --- lock (stale after 30 minutes) -------------------------------------------
if ! mkdir "$LOCK" 2>/dev/null; then
  if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +30 2>/dev/null)" ]; then
    rmdir "$LOCK" 2>/dev/null
    mkdir "$LOCK" 2>/dev/null || { say "self-update: lock held; aborting" >&2; exit 3; }
  else
    say "self-update: lock held; aborting" >&2
    exit 3
  fi
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

REPO="$(resolve_repo)"

# --- preconditions ------------------------------------------------------------
if [ ! -d "$REPO/.git" ]; then
  log "abort reason=no-repo repo=$REPO"
  say "self-update: no git repo at $REPO (set CCC_SELF_UPDATE_REPO or $REPO_FILE)" >&2
  exit 4
fi
CUR_BRANCH="$(git -C "$REPO" symbolic-ref --short HEAD 2>/dev/null || echo '?')"
if [ "$CUR_BRANCH" != "$BRANCH" ]; then
  log "abort reason=wrong-branch branch=$CUR_BRANCH expected=$BRANCH"
  say "self-update: repo is on '$CUR_BRANCH', expected '$BRANCH'; aborting (fail-closed)" >&2
  exit 4
fi
if [ -n "$(git -C "$REPO" status --porcelain 2>/dev/null)" ]; then
  log "abort reason=dirty-tree repo=$REPO"
  say "self-update: working tree not clean; aborting (fail-closed)" >&2
  exit 4
fi

OLD_SHA="$(git -C "$REPO" rev-parse HEAD 2>/dev/null)"

# --- fetch + ff-only merge ----------------------------------------------------
if ! git -C "$REPO" fetch origin "$BRANCH" >/dev/null 2>&1; then
  log "abort reason=fetch-failed repo=$REPO"
  say "self-update: git fetch failed" >&2
  exit 5
fi
if ! git -C "$REPO" merge --ff-only "origin/$BRANCH" >/dev/null 2>&1; then
  log "abort reason=non-ff repo=$REPO"
  say "self-update: local branch diverged from origin/$BRANCH (non-ff); aborting (fail-closed)" >&2
  exit 5
fi
NEW_SHA="$(git -C "$REPO" rev-parse HEAD 2>/dev/null)"

CHANGED=false
[ "$OLD_SHA" != "$NEW_SHA" ] && CHANGED=true

if [ "$CHANGED" = "false" ] && [ "$FORCE" != "1" ]; then
  log "done result=up-to-date sha=$NEW_SHA"
  say "self-update: already up to date ($(git -C "$REPO" rev-parse --short HEAD))"
  exit 0
fi

# --- redeploy harness ---------------------------------------------------------
SETUP_OK=true
if ! (cd "$REPO" && bash setup.sh >>"$LOG" 2>&1); then
  SETUP_OK=false
  git -C "$REPO" reset --hard "$OLD_SHA" >/dev/null 2>&1
  audit "setup-failed-rolled-back" "$OLD_SHA" "$NEW_SHA" "$CHANGED" false '[]'
  notify "self-update 실패: setup.sh 오류 — ${OLD_SHA:0:7}로 롤백했습니다. 로그: ~/.claude/state/self-update.log" "fail-$NEW_SHA"
  say "self-update: setup.sh failed; rolled back to ${OLD_SHA:0:7}" >&2
  exit 6
fi

# --- restart allowlisted services ----------------------------------------------
SERVICES_JSON='[]'
FAILED=0
RESTARTED=0
if [ -f "$SERVICES_FILE" ]; then
  while IFS= read -r svc; do
    svc="${svc%%#*}"; svc="$(printf '%s' "$svc" | tr -d '[:space:]')"
    [ -n "$svc" ] || continue
    if ! printf '%s' "$svc" | grep -Eq '^[A-Za-z0-9@._:-]+$'; then
      log "service skipped reason=invalid-name name=$svc"
      continue
    fi
    ok=true
    if "$SYSTEMCTL" restart "$svc" >>"$LOG" 2>&1; then
      i=0
      until "$SYSTEMCTL" is-active --quiet "$svc" 2>/dev/null; do
        i=$((i + 1)); [ "$i" -ge 10 ] && { ok=false; break; }
        sleep 1
      done
    else
      ok=false
    fi
    [ "$ok" = "true" ] && RESTARTED=$((RESTARTED + 1)) || FAILED=$((FAILED + 1))
    SERVICES_JSON="$(printf '%s' "$SERVICES_JSON" | jq -c --arg n "$svc" --argjson ok "$ok" '. + [{name:$n, ok:$ok}]')"
    log "service name=$svc ok=$ok"
  done < "$SERVICES_FILE"
else
  log "restart skipped reason=no-services-file path=$SERVICES_FILE"
fi

SHORT_NEW="$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null)"
if [ "$FAILED" -gt 0 ]; then
  audit "restart-failures" "$OLD_SHA" "$NEW_SHA" "$CHANGED" "$SETUP_OK" "$SERVICES_JSON"
  notify "self-update ${SHORT_NEW}: 서비스 ${FAILED}개 재시작 실패 (${RESTARTED}개 성공). ~/.claude/state/self-update.log 확인 필요." "fail-$NEW_SHA"
  say "self-update: updated to $SHORT_NEW but $FAILED service(s) failed to restart" >&2
  exit 7
fi

audit "ok" "$OLD_SHA" "$NEW_SHA" "$CHANGED" "$SETUP_OK" "$SERVICES_JSON"
if [ "$CHANGED" = "true" ]; then
  notify "self-update 완료: ${OLD_SHA:0:7} → ${SHORT_NEW}, 서비스 ${RESTARTED}개 재시작." "ok-$NEW_SHA"
fi
say "self-update: ok (${OLD_SHA:0:7} → ${SHORT_NEW}, services restarted: $RESTARTED)"
exit 0
