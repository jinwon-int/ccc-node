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
#   4. if HEAD changed (or --force): snapshot Claude + Hermes managed artifacts,
#      run ./setup.sh, then verify both repo SHA and artifact rollback on failure
#   5. restart each allowlisted service and verify it is active again
#   6. append a JSONL audit record and queue an owner Telegram notification
#      (spool only — this script never touches the bot token)
#
# Modes: run [--force] | status
# Env: CCC_SELF_UPDATE_REPO, CCC_SELF_UPDATE_BRANCH (default main),
#      CCC_SELF_UPDATE_SYSTEMCTL (default systemctl; tests inject a fake),
#      CCC_STATE_DIR, CCC_PUSH_SPOOL, CCC_NODE.
# Idle gate: before touching anything the run defers (exit 8) while the telegram
#      bridge is serving a request, so a restart cannot SIGTERM-kill an in-flight
#      `claude` child (exit 143) mid-task. Reads the bridge's health.json.
#      CCC_SELF_UPDATE_HEALTH_FILE (default ~/.telegram_bot/health.json),
#      CCC_SELF_UPDATE_HEALTH_FRESH_SECONDS (90), CCC_SELF_UPDATE_BUSY_MAX_SECONDS
#      (1800 — never defer a task older than this), CCC_SELF_UPDATE_MAX_DEFER_SECONDS
#      (3600 — cap total deferral so continuous load can't starve updates).
#      Fail-open (missing/unreadable/stale health → proceed); --force bypasses.
# Exit: 0 = up-to-date or updated cleanly; 8 = deferred (bridge busy); other
#      non-zero = aborted (reason logged).
set -uo pipefail

CLAUDE_DIR="${CCC_CLAUDE_DIR:-${HOME:-/root}/.claude}"
HERMES_ROOT="${CCC_HERMES_DIR:-${HOME:-/root}/.hermes}"
STATE_DIR="${CCC_STATE_DIR:-$CLAUDE_DIR/state}"
LOG="$STATE_DIR/self-update.log"
LOCK="$STATE_DIR/self-update.lock"
SPOOL="${CCC_PUSH_SPOOL:-$STATE_DIR/telegram-spool}"
SERVICES_FILE="${CCC_SELF_UPDATE_SERVICES:-$CLAUDE_DIR/self-update.services}"
REPO_FILE="$CLAUDE_DIR/self-update.repo"
BRANCH="${CCC_SELF_UPDATE_BRANCH:-main}"
SYSTEMCTL="${CCC_SELF_UPDATE_SYSTEMCTL:-systemctl}"

SELF_UPDATE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)"
HARNESS_PATHS_LIB="$SELF_UPDATE_DIR/lib/harness-paths.sh"
if [ ! -r "$HARNESS_PATHS_LIB" ]; then
  printf '%s\n' "self-update: shared harness path library is missing: $HARNESS_PATHS_LIB" >&2
  exit 4
fi
# shellcheck source=/dev/null
. "$HARNESS_PATHS_LIB"

ccc_validate_self_update_roots "$CLAUDE_DIR" "$HERMES_ROOT" "$STATE_DIR" || exit 4
mkdir -p "$STATE_DIR" 2>/dev/null
INSTALL_SNAPSHOT_DIR=""
CLAUDE_SNAPSHOT=""
HERMES_SNAPSHOT=""
KEEP_INSTALL_SNAPSHOT=0

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

snapshot_installed_artifacts() {
  local existing=() item
  ccc_validate_managed_artifacts "self-update:" "$CLAUDE_DIR" "$HERMES_ROOT" "${CCC_MANAGED_PATHS[@]}" || return 1
  INSTALL_SNAPSHOT_DIR="$(mktemp -d "$STATE_DIR/self-update-install-rollback.XXXXXX")" || return 1
  chmod 700 "$INSTALL_SNAPSHOT_DIR" || return 1
  CLAUDE_SNAPSHOT="$INSTALL_SNAPSHOT_DIR/claude.tar.gz"
  HERMES_SNAPSHOT="$INSTALL_SNAPSHOT_DIR/hermes.tar.gz"
  for item in "${CCC_MANAGED_PATHS[@]}"; do
    { [ -e "$CLAUDE_DIR/$item" ] || [ -L "$CLAUDE_DIR/$item" ]; } && existing+=("$item")
  done
  if [ "${#existing[@]}" -gt 0 ]; then
    (umask 077; tar -czf "$CLAUDE_SNAPSHOT" -C "$CLAUDE_DIR" "${existing[@]}") || return 1
  else
    (umask 077; tar -czf "$CLAUDE_SNAPSHOT" --files-from /dev/null) || return 1
  fi
  if [ -e "$HERMES_ROOT/honcho.json" ] || [ -L "$HERMES_ROOT/honcho.json" ]; then
    (umask 077; tar -czf "$HERMES_SNAPSHOT" -C "$HERMES_ROOT" honcho.json) || return 1
  else
    (umask 077; tar -czf "$HERMES_SNAPSHOT" --files-from /dev/null) || return 1
  fi
  chmod 600 "$CLAUDE_SNAPSHOT" "$HERMES_SNAPSHOT" || return 1
  tar -tzf "$CLAUDE_SNAPSHOT" >/dev/null || return 1
  tar -tzf "$HERMES_SNAPSHOT" >/dev/null || return 1
  python3 - "$CLAUDE_SNAPSHOT" "$HERMES_SNAPSHOT" "${CCC_MANAGED_PATHS[*]}" <<'PY' || return 1
import pathlib
import sys
import tarfile

claude_archive, hermes_archive, allowed_text = sys.argv[1:]
for archive, allowed in (
    (claude_archive, set(allowed_text.split())),
    (hermes_archive, {"honcho.json"}),
):
    with tarfile.open(archive, "r:gz") as tf:
        for member in tf.getmembers():
            path = pathlib.PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] not in allowed:
                raise SystemExit(f"unsafe snapshot member: {member.name}")
            if member.issym() or member.islnk():
                raise SystemExit(f"unsafe snapshot link member: {member.name}")
PY
}

restore_installed_artifacts() {
  local item failed=0
  for item in "${CCC_MANAGED_PATHS[@]}"; do
    rm -rf -- "$CLAUDE_DIR/$item" || failed=1
  done
  mkdir -p "$CLAUDE_DIR" "$HERMES_ROOT" || failed=1
  tar -xzf "$CLAUDE_SNAPSHOT" -C "$CLAUDE_DIR" || failed=1
  rm -f -- "$HERMES_ROOT/honcho.json" || failed=1
  tar -xzf "$HERMES_SNAPSHOT" -C "$HERMES_ROOT" || failed=1
  [ "$failed" = 0 ]
}

cleanup() {
  if [ "$KEEP_INSTALL_SNAPSHOT" != 1 ] && [ -n "$INSTALL_SNAPSHOT_DIR" ]; then
    rm -rf -- "$INSTALL_SNAPSHOT_DIR"
  fi
  rmdir "$LOCK" 2>/dev/null
}

reset_repo_to_old_sha() {
  git -C "$REPO" reset --hard "$OLD_SHA" >/dev/null 2>&1 || return 1
  [ "$(git -C "$REPO" rev-parse HEAD 2>/dev/null)" = "$OLD_SHA" ] || return 1
  [ -z "$(git -C "$REPO" status --porcelain 2>/dev/null)" ]
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
trap cleanup EXIT

# --- idle gate: never restart the bridge while it is serving a request --------
# The bridge writes an in-flight workload snapshot to its health.json. Restarting
# it mid-request SIGTERM-kills the in-flight `claude` child (exit 143) and destroys
# the user's work. When the bridge is busy we defer the WHOLE run (nothing fetched
# or restarted) and let the next scheduled tick retry — bounded so a hung/very-long
# request, or continuous load, cannot starve updates forever.
HEALTH_FILE="${CCC_SELF_UPDATE_HEALTH_FILE:-${HOME:-/root}/.telegram_bot/health.json}"
FRESH_SECONDS="${CCC_SELF_UPDATE_HEALTH_FRESH_SECONDS:-90}"
BUSY_MAX_SECONDS="${CCC_SELF_UPDATE_BUSY_MAX_SECONDS:-1800}"
MAX_DEFER_SECONDS="${CCC_SELF_UPDATE_MAX_DEFER_SECONDS:-3600}"
DEFER_MARK="$STATE_DIR/self-update.deferred-since"

# Echo a reason and return 0 when the bridge is busy; return 1 (fail-open) when
# idle, unknown, stale, or over the per-task cap.
bridge_is_busy() {
  [ -f "$HEALTH_FILE" ] || return 1
  python3 - "$HEALTH_FILE" "$FRESH_SECONDS" "$BUSY_MAX_SECONDS" <<'PY'
import json, sys
from datetime import datetime, timezone
path, fresh_window, busy_max = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
try:
    d = json.load(open(path, encoding="utf-8"))
except Exception:
    sys.exit(1)  # unreadable -> fail-open (treat as idle)
wl = d.get("workload") or {}
try:
    active = int(wl.get("active_requests") or 0)
    oldest = float(wl.get("oldest_request_age_seconds") or 0)
except Exception:
    sys.exit(1)
ua = d.get("updated_at")
fresh = False
if ua:
    try:
        t = datetime.fromisoformat(str(ua).replace("Z", "+00:00"))
        fresh = (datetime.now(timezone.utc) - t).total_seconds() <= fresh_window
    except Exception:
        fresh = False
if fresh and active > 0 and oldest < busy_max:
    print("active=%d oldest=%ds" % (active, int(oldest)))
    sys.exit(0)  # busy
sys.exit(1)  # idle / stale / over-cap -> proceed
PY
}

if [ "$FORCE" != "1" ] && busy_reason="$(bridge_is_busy)"; then
  now_epoch="$(date +%s)"
  since="$(cat "$DEFER_MARK" 2>/dev/null)"
  case "$since" in ''|*[!0-9]*) since="" ;; esac
  [ -n "$since" ] || { since="$now_epoch"; printf '%s' "$now_epoch" > "$DEFER_MARK" 2>/dev/null; }
  waited=$(( now_epoch - since ))
  if [ "$waited" -lt "$MAX_DEFER_SECONDS" ]; then
    log "deferred reason=bridge-busy $busy_reason waited=${waited}s"
    say "self-update: bridge busy ($busy_reason) — deferring, will retry next tick"
    exit 8
  fi
  log "proceed reason=defer-cap-exceeded waited=${waited}s $busy_reason"
  say "self-update: bridge busy but deferred ${waited}s ≥ ${MAX_DEFER_SECONDS}s cap — proceeding"
fi
# Not busy (or forced, or cap exceeded) → clear any deferral marker and continue.
rm -f "$DEFER_MARK" 2>/dev/null

REPO="$(resolve_repo)"

ccc_validate_self_update_repo "$REPO" "$CLAUDE_DIR" "$HERMES_ROOT" || exit 4

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
if ! snapshot_installed_artifacts; then
  if reset_repo_to_old_sha; then
    audit "artifact-snapshot-failed" "$OLD_SHA" "$NEW_SHA" "$CHANGED" false '[]'
    notify "self-update 실패: 설치본 rollback snapshot 생성 실패. repo는 이전 SHA로 복구했습니다. 로그: ~/.claude/state/self-update.log" "snapshot-fail-$NEW_SHA"
    say "self-update: installed-artifact snapshot failed; repository rolled back before setup" >&2
    exit 6
  fi
  audit "artifact-snapshot-failed-repo-rollback-degraded" "$OLD_SHA" "$NEW_SHA" "$CHANGED" false '[]'
  notify "self-update 중대 실패: snapshot 생성과 repo rollback이 모두 실패했습니다. 로그를 즉시 확인하세요." "snapshot-repo-degraded-$NEW_SHA"
  say "self-update: snapshot failed and repository rollback was degraded" >&2
  exit 9
fi
if ! (cd "$REPO" && bash setup.sh >>"$LOG" 2>&1); then
  SETUP_OK=false
  REPO_ROLLBACK_OK=true
  ARTIFACT_ROLLBACK_OK=true
  reset_repo_to_old_sha || REPO_ROLLBACK_OK=false
  restore_installed_artifacts || ARTIFACT_ROLLBACK_OK=false
  if [ "$REPO_ROLLBACK_OK" = true ] && [ "$ARTIFACT_ROLLBACK_OK" = true ]; then
    audit "setup-failed-rolled-back" "$OLD_SHA" "$NEW_SHA" "$CHANGED" false '[]'
    notify "self-update 실패: setup.sh 오류 — repo와 설치본을 ${OLD_SHA:0:7} 상태로 롤백했습니다. 로그: ~/.claude/state/self-update.log" "fail-$NEW_SHA"
    say "self-update: setup.sh failed; rolled back repo and installed artifacts to ${OLD_SHA:0:7}" >&2
    exit 6
  fi
  audit "setup-failed-rollback-degraded" "$OLD_SHA" "$NEW_SHA" "$CHANGED" false '[]'
  KEEP_INSTALL_SNAPSHOT=1
  log "recovery snapshot=$INSTALL_SNAPSHOT_DIR repoRollback=$REPO_ROLLBACK_OK artifactRollback=$ARTIFACT_ROLLBACK_OK"
  notify "self-update 중대 실패: setup.sh 오류 뒤 rollback이 불완전합니다. 로그를 즉시 확인하세요." "rollback-degraded-$NEW_SHA"
  say "self-update: setup failed and rollback was degraded; recovery snapshot retained at $INSTALL_SNAPSHOT_DIR" >&2
  exit 9
fi
rm -rf -- "$INSTALL_SNAPSHOT_DIR"
INSTALL_SNAPSHOT_DIR=""

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
