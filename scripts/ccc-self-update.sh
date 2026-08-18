#!/usr/bin/env bash
# ccc-self-update — the PRE-APPROVED node maintenance procedure.
#
# Problem this solves: a fleet node needs to pick up ccc-node updates and
# restart its own services unattended, but service restarts are otherwise
# fresh-approval behavioral policy. This script IS the standing approval: a
# fixed, code-reviewed, audited procedure (mirroring the ccc-telegram-bridge
# restart carve-out rationale) that an agent may invoke as a whole. The blast
# radius stays operator-controlled because the ONLY services it will ever touch
# are the ones listed in an operator-owned allowlist file the agent must not
# write:
#   ~/.claude/self-update.services   ([user:|system:]unit per line, # comments)
#   ~/.claude/self-update.repo       (optional: absolute repo path override)
#   ~/.claude/self-update.restart-cmd (optional: one external restart command
#      for hosts where systemd cannot reach the bridge, e.g. Termux
#      `bridge/start.sh --path "$HOME" --restart -d`. Runs INSIDE this script's
#      audit/notify boundary so its failure can never be discarded the way the
#      hand-chained cron `... ; exit 0` discarded it on daegyo (#971).)
#   ~/.claude/self-update.health-cmd  (optional: one runtime health probe, exit
#      0 = healthy. With both files present, an up-to-date tick that finds the
#      runtime DOWN attempts one recovery restart — so the second daily slot
#      can recover an updated-but-down node (#971).)
#   ~/.claude/self-update.no-reapply (optional: operator kill-switch; when this
#      file exists, installer-managed cron is never rewritten. Env override:
#      CCC_SELF_UPDATE_REAPPLY=0. Agent must not write the file.)
#
# Procedure (run):
#   1. take a lock; resolve the repo (env > repo file > script location > ~/ccc-node)
#   2. preconditions: .git present, clean working tree, on the expected branch
#   3. git fetch + merge --ff-only (never rewrites local history)
#   4. if HEAD changed (or --force): snapshot Claude + Hermes managed artifacts
#      and the Codex GitHub policy config, run ./setup.sh, validate bridge
#      runtime config when its service is allowlisted, then verify repo SHA
#      and artifact rollback on failure
#   5. if any install record's gen stamp drifted: snapshot crontab, replay
#      the recorded argv, verify the new gen; on failure restore crontab and
#      abort (exit 12). Kill-switch: self-update.no-reapply or
#      CCC_SELF_UPDATE_REAPPLY=0. Only runs when HEAD changed (or --force).
#   6. restart each allowlisted service and verify it is active again
#   7. append a JSONL audit record and queue an owner Telegram notification
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
# Exit: 0 = up-to-date or updated cleanly; 7 = a restart (allowlisted service
#      or external restart-cmd) or a recovery attempt failed; 8 = deferred
#      (bridge busy); 11 = degraded (code updated but nothing restarted and no
#      restart-cmd configured); 12 = installer re-apply failed (crontab was
#      restored); other non-zero = aborted (reason logged).
set -uo pipefail

CLAUDE_DIR="${CCC_CLAUDE_DIR:-${HOME:-/root}/.claude}"
HERMES_ROOT="${CCC_HERMES_DIR:-${HOME:-/root}/.hermes}"
CODEX_DIR="${CODEX_HOME:-${HOME:-/root}/.codex}"
STATE_DIR="${CCC_STATE_DIR:-$CLAUDE_DIR/state}"
LOG="$STATE_DIR/self-update.log"
LOCK="$STATE_DIR/self-update.lock"
SPOOL="${CCC_PUSH_SPOOL:-$STATE_DIR/telegram-spool}"
SERVICES_FILE="${CCC_SELF_UPDATE_SERVICES:-$CLAUDE_DIR/self-update.services}"
REPO_FILE="$CLAUDE_DIR/self-update.repo"
RESTART_CMD_FILE="${CCC_SELF_UPDATE_RESTART_CMD_FILE:-$CLAUDE_DIR/self-update.restart-cmd}"
HEALTH_CMD_FILE="${CCC_SELF_UPDATE_HEALTH_CMD_FILE:-$CLAUDE_DIR/self-update.health-cmd}"
RESTART_WAIT_SECONDS="${CCC_SELF_UPDATE_RESTART_WAIT_SECONDS:-60}"
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
CODEX_SNAPSHOT=""
KEEP_INSTALL_SNAPSHOT=0

ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() { printf '%s %s\n' "$(ts)" "$*" 2>/dev/null >> "$LOG" || :; }
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
    2>/dev/null >> "$LOG" || :
}

read_operator_cmd() { # <file> — first non-comment, non-blank line (operator-owned)
  [ -f "$1" ] || return 1
  local line
  while IFS= read -r line; do
    line="${line%%#*}"
    line="$(printf '%s' "$line" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    [ -n "$line" ] && { printf '%s' "$line"; return 0; }
  done < "$1"
  return 1
}

resolve_restart_cmd() {
  if [ -n "${CCC_SELF_UPDATE_RESTART_CMD:-}" ]; then printf '%s' "$CCC_SELF_UPDATE_RESTART_CMD"; return 0; fi
  read_operator_cmd "$RESTART_CMD_FILE"
}

resolve_health_cmd() {
  if [ -n "${CCC_SELF_UPDATE_HEALTH_CMD:-}" ]; then printf '%s' "$CCC_SELF_UPDATE_HEALTH_CMD"; return 0; fi
  read_operator_cmd "$HEALTH_CMD_FILE"
}

# Run the operator's external restart command INSIDE the audit/notify boundary.
# Outcome: health-cmd poll (when configured, up to RESTART_WAIT_SECONDS), else
# the command's own exit code. Returns 0 = runtime back, 1 = still down.
run_external_restart() {
  local rcmd hcmd rc waited
  rcmd="$(resolve_restart_cmd)" || return 1
  hcmd="$(resolve_health_cmd || true)"
  log "external-restart begin"
  if command -v timeout >/dev/null 2>&1; then
    timeout 180 bash -c "$rcmd" >>"$LOG" 2>&1
  else
    bash -c "$rcmd" >>"$LOG" 2>&1
  fi
  rc=$?
  log "external-restart exit=$rc"
  if [ -n "$hcmd" ]; then
    waited=0
    until bash -c "$hcmd" >>"$LOG" 2>&1; do
      waited=$((waited + 3))
      [ "$waited" -ge "$RESTART_WAIT_SECONDS" ] && { log "external-restart health-timeout waited=${waited}s"; return 1; }
      sleep 3
    done
    log "external-restart healthy waited=${waited}s"
    return 0
  fi
  return "$rc"
}

snapshot_installed_artifacts() {
  local existing=() item
  ccc_validate_managed_artifacts "self-update:" "$CLAUDE_DIR" "$HERMES_ROOT" "${CCC_MANAGED_PATHS[@]}" || return 1
  INSTALL_SNAPSHOT_DIR="$(mktemp -d "$STATE_DIR/self-update-install-rollback.XXXXXX")" || return 1
  chmod 700 "$INSTALL_SNAPSHOT_DIR" || return 1
  CLAUDE_SNAPSHOT="$INSTALL_SNAPSHOT_DIR/claude.tar.gz"
  HERMES_SNAPSHOT="$INSTALL_SNAPSHOT_DIR/hermes.tar.gz"
  CODEX_SNAPSHOT="$INSTALL_SNAPSHOT_DIR/codex.tar.gz"
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
  # The Codex GitHub policy state lives outside $CLAUDE_DIR and setup.sh
  # replaces config.toml with no backup of its own (#1131); capture it so a
  # rollback does not strand the new policy while claiming a full restore.
  ccc_snapshot_codex_policy_state "$CODEX_DIR" "$INSTALL_SNAPSHOT_DIR" || return 1
  chmod 600 "$CODEX_SNAPSHOT" || return 1
  python3 - "$CLAUDE_SNAPSHOT" "$HERMES_SNAPSHOT" "$CODEX_SNAPSHOT" "${CCC_MANAGED_PATHS[*]}" <<'PY' || return 1
import pathlib
import sys
import tarfile

claude_archive, hermes_archive, codex_archive, allowed_text = sys.argv[1:]
for archive, allowed in (
    (claude_archive, set(allowed_text.split())),
    (hermes_archive, {"honcho.json"}),
    (codex_archive, {"config.toml"}),
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
  ccc_restore_codex_policy_state "$CODEX_DIR" "$INSTALL_SNAPSHOT_DIR" || failed=1
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

bridge_service_allowlisted() {
  local svc
  [ -f "$SERVICES_FILE" ] || return 1
  while IFS= read -r svc; do
    svc="${svc%%#*}"
    svc="$(printf '%s' "$svc" | tr -d '[:space:]')"
    case "$svc" in
      user:*) svc="${svc#user:}" ;;
      system:*) svc="${svc#system:}" ;;
    esac
    case "$svc" in
      ccc-telegram-bridge|ccc-telegram-bridge.service) return 0 ;;
    esac
  done < "$SERVICES_FILE"
  return 1
}

bridge_runtime_config_preflight() {
  local checker project_root
  checker="$REPO/bridge/runtime_config_check.py"
  project_root="${CCC_SELF_UPDATE_BRIDGE_PROJECT_ROOT:-${HOME:-/root}}"
  [ -f "$checker" ] || {
    log "bridge-config-preflight result=missing-checker"
    return 1
  }
  if python3 "$checker" --project-root "$project_root" \
      --bridge-env "$REPO/bridge/.env" --json >>"$LOG" 2>&1; then
    log "bridge-config-preflight result=ok"
    return 0
  fi
  log "bridge-config-preflight result=invalid"
  return 1
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
# A precondition abort is TERMINAL: nothing about it self-heals on the next tick,
# so the node stops updating until a human intervenes. Before #1060 these paths
# only wrote to the local log — the operator learned about the stall by manually
# probing the fleet, and seoseo sat 23h behind main with no alert. Every terminal
# abort therefore notifies. The dedup key is the reason, not a SHA: the SHA not
# moving IS the failure, so a SHA-keyed alert would describe a different incident
# each time it fired. push_notifier drops repeats inside a 300s window, so the
# scheduled 04:45/05:45 ticks yield at most one alert apiece.
notify_stalled() { # <reason> <text>
  log "abort reason=$1 repo=$REPO"
  notify "$2 ~/.claude/state/self-update.log" "stalled-$1"
}
if [ ! -d "$REPO/.git" ]; then
  notify_stalled no-repo "self-update 정지: $REPO 에 git 저장소가 없습니다. 이 노드는 복구 전까지 갱신되지 않습니다."
  say "self-update: no git repo at $REPO (set CCC_SELF_UPDATE_REPO or $REPO_FILE)" >&2
  exit 4
fi
CUR_BRANCH="$(git -C "$REPO" symbolic-ref --short HEAD 2>/dev/null || echo '?')"
if [ "$CUR_BRANCH" != "$BRANCH" ]; then
  notify_stalled wrong-branch "self-update 정지: 레포가 '$CUR_BRANCH' 브랜치에 있습니다 (기대: '$BRANCH'). 이 노드는 복구 전까지 갱신되지 않습니다 — 관리 체크아웃은 '$BRANCH' 고정, 개발은 git worktree로 분리하세요."
  say "self-update: repo is on '$CUR_BRANCH', expected '$BRANCH'; aborting (fail-closed)" >&2
  exit 4
fi
if [ -n "$(git -C "$REPO" status --porcelain 2>/dev/null)" ]; then
  notify_stalled dirty-tree "self-update 정지: $REPO 작업 트리에 미커밋 변경이 있습니다. 이 노드는 복구 전까지 갱신되지 않습니다."
  say "self-update: working tree not clean; aborting (fail-closed)" >&2
  exit 4
fi

OLD_SHA="$(git -C "$REPO" rev-parse HEAD 2>/dev/null)"

# --- fetch + ff-only merge ----------------------------------------------------
# fetch failure is the one precondition that DOES self-heal (transient network),
# so it alerts only once it has burned consecutive scheduled ticks.
FETCH_FAIL_FILE="$STATE_DIR/self-update.fetch-failures"
FETCH_FAIL_ALERT_AFTER="${CCC_SELF_UPDATE_FETCH_FAIL_ALERT_AFTER:-2}"
if ! git -C "$REPO" fetch origin "$BRANCH" >/dev/null 2>&1; then
  fetch_fails="$(cat "$FETCH_FAIL_FILE" 2>/dev/null)"
  case "$fetch_fails" in ''|*[!0-9]*) fetch_fails=0 ;; esac
  fetch_fails=$(( fetch_fails + 1 ))
  printf '%s' "$fetch_fails" > "$FETCH_FAIL_FILE" 2>/dev/null || :
  log "abort reason=fetch-failed repo=$REPO consecutive=$fetch_fails"
  if [ "$fetch_fails" -ge "$FETCH_FAIL_ALERT_AFTER" ]; then
    notify "self-update 정지: git fetch가 ${fetch_fails}회 연속 실패했습니다. 이 노드는 복구 전까지 갱신되지 않습니다. ~/.claude/state/self-update.log" "stalled-fetch-failed"
  fi
  say "self-update: git fetch failed" >&2
  exit 5
fi
rm -f "$FETCH_FAIL_FILE" 2>/dev/null || :
if ! git -C "$REPO" merge --ff-only "origin/$BRANCH" >/dev/null 2>&1; then
  notify_stalled non-ff "self-update 정지: 로컬 브랜치가 origin/$BRANCH 와 분기했습니다 (non-ff). 이 노드는 복구 전까지 갱신되지 않습니다."
  say "self-update: local branch diverged from origin/$BRANCH (non-ff); aborting (fail-closed)" >&2
  exit 5
fi
NEW_SHA="$(git -C "$REPO" rev-parse HEAD 2>/dev/null)"

CHANGED=false
[ "$OLD_SHA" != "$NEW_SHA" ] && CHANGED=true

if [ "$CHANGED" = "false" ] && [ "$FORCE" != "1" ]; then
  # Second-slot runtime recovery (#971): code is current, but an earlier
  # chained restart may have failed and left the runtime down. When the
  # operator configured both a health probe and an external restart command,
  # verify runtime health and attempt ONE recovery restart — with the outcome
  # audited and notified, never discarded.
  if hcmd="$(resolve_health_cmd)" && resolve_restart_cmd >/dev/null 2>&1; then
    if bash -c "$hcmd" >>"$LOG" 2>&1; then
      log "done result=up-to-date sha=$NEW_SHA runtime=healthy"
      say "self-update: already up to date ($(git -C "$REPO" rev-parse --short HEAD))"
      exit 0
    fi
    SHORT_CUR="$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null)"
    log "runtime unhealthy at up-to-date tick; attempting recovery restart"
    if run_external_restart; then
      audit "runtime-recovered" "$OLD_SHA" "$NEW_SHA" "$CHANGED" true '[{"name":"external-restart","ok":true,"scope":"external"}]'
      notify "self-update ${SHORT_CUR}: 코드는 최신이나 런타임 다운 감지 — 외부 재시작으로 복구 완료. ~/.claude/state/self-update.log" "recovered-$NEW_SHA"
      say "self-update: code up to date but runtime was down; recovered via external restart"
      exit 0
    fi
    audit "runtime-down" "$OLD_SHA" "$NEW_SHA" "$CHANGED" true '[{"name":"external-restart","ok":false,"scope":"external"}]'
    notify "self-update ${SHORT_CUR} 경고: 코드는 최신이나 런타임이 다운 상태이며 복구 재시작도 실패했습니다. 브리지가 남아있는지 즉시 확인 필요. ~/.claude/state/self-update.log" "runtime-down-$NEW_SHA"
    say "self-update: code up to date but runtime is DOWN and the recovery restart failed" >&2
    exit 7
  fi
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
    notify "self-update 실패: setup.sh 오류 — repo와 설치본(Claude 하네스·honcho.json·Codex GitHub 정책 설정)을 ${OLD_SHA:0:7} 상태로 롤백했습니다. 로그: ~/.claude/state/self-update.log" "fail-$NEW_SHA"
    say "self-update: setup.sh failed; rolled back repo and installed artifacts (Claude harness, honcho.json, Codex GitHub policy config) to ${OLD_SHA:0:7}" >&2
    exit 6
  fi
  audit "setup-failed-rollback-degraded" "$OLD_SHA" "$NEW_SHA" "$CHANGED" false '[]'
  KEEP_INSTALL_SNAPSHOT=1
  log "recovery snapshot=$INSTALL_SNAPSHOT_DIR repoRollback=$REPO_ROLLBACK_OK artifactRollback=$ARTIFACT_ROLLBACK_OK"
  notify "self-update 중대 실패: setup.sh 오류 뒤 rollback이 불완전합니다. 로그를 즉시 확인하세요." "rollback-degraded-$NEW_SHA"
  say "self-update: setup failed and rollback was degraded; recovery snapshot retained at $INSTALL_SNAPSHOT_DIR" >&2
  exit 9
fi
if bridge_service_allowlisted && ! bridge_runtime_config_preflight; then
  SETUP_OK=false
  REPO_ROLLBACK_OK=true
  ARTIFACT_ROLLBACK_OK=true
  reset_repo_to_old_sha || REPO_ROLLBACK_OK=false
  restore_installed_artifacts || ARTIFACT_ROLLBACK_OK=false
  if [ "$REPO_ROLLBACK_OK" = true ] && [ "$ARTIFACT_ROLLBACK_OK" = true ]; then
    audit "bridge-config-preflight-failed-rolled-back" "$OLD_SHA" "$NEW_SHA" "$CHANGED" false '[]'
    notify "self-update 실패: bridge runtime config preflight 오류 — repo와 설치본(Claude 하네스·honcho.json·Codex GitHub 정책 설정)을 ${OLD_SHA:0:7} 상태로 롤백했습니다. 로그: ~/.claude/state/self-update.log" "bridge-config-fail-$NEW_SHA"
    say "self-update: bridge runtime config preflight failed; rolled back before service restart" >&2
    exit 6
  fi
  audit "bridge-config-preflight-failed-rollback-degraded" "$OLD_SHA" "$NEW_SHA" "$CHANGED" false '[]'
  KEEP_INSTALL_SNAPSHOT=1
  log "recovery snapshot=$INSTALL_SNAPSHOT_DIR repoRollback=$REPO_ROLLBACK_OK artifactRollback=$ARTIFACT_ROLLBACK_OK"
  notify "self-update 중대 실패: bridge runtime config 오류 뒤 rollback이 불완전합니다. 로그를 즉시 확인하세요." "bridge-config-rollback-degraded-$NEW_SHA"
  say "self-update: bridge runtime config preflight failed and rollback was degraded" >&2
  exit 9
fi
# The recovery snapshot deliberately outlives setup and the runtime-config
# preflight: a service that fails to come back is exactly when rollback
# material is needed, and deleting it here left that path with nothing to
# restore from. It is removed once the restarts have succeeded (below), so the
# success path keeps its no-residue behavior.

# --- installer re-apply (cron drift repair, #1081 phase 2) --------------------
# Runs only on a changed (or --force) tick: an up-to-date node has the same
# installer bytes it last applied, so gen cannot drift. Doctor still surfaces
# unstamped/legacy entries on every run; the next code change repairs them.
REAPPLY_COUNT=0
REAPPLY_NOTE=""
CRONTAB_CMD="${CCC_SELF_UPDATE_CRONTAB_CMD:-crontab}"
NO_REAPPLY_FILE="$CLAUDE_DIR/self-update.no-reapply"
CRONTAB_SNAP=""
reapply_skip() { log "reapply skipped reason=$1"; }
if [ "${CCC_SELF_UPDATE_REAPPLY:-1}" = "0" ]; then
  reapply_skip env-disabled
elif [ -f "$NO_REAPPLY_FILE" ]; then
  reapply_skip operator-file
elif ! command -v "${CRONTAB_CMD%% *}" >/dev/null 2>&1; then
  reapply_skip no-crontab
elif [ ! -r "$REPO/scripts/lib/installer-gen-stamp.sh" ]; then
  reapply_skip lib-missing
else
  # shellcheck source=/dev/null
  . "$REPO/scripts/lib/installer-gen-stamp.sh"
  for rec in "$STATE_DIR"/install-*.json; do
    [ -f "$rec" ] || continue
    installer="$(jq -r '.installer // empty' "$rec" 2>/dev/null)" || installer=""
    marker="$(jq -r '.marker // empty' "$rec" 2>/dev/null)" || marker=""
    old_gen="$(jq -r '.gen // empty' "$rec" 2>/dev/null)" || old_gen=""
    schema="$(jq -r '.schema // empty' "$rec" 2>/dev/null)" || schema=""
    case "$schema" in ccc.install-record.v1) ;; *) log "reapply skip reason=bad-schema path=$rec"; continue ;; esac
    case "$installer" in
      scripts/install-[A-Za-z0-9._-]*\.sh) ;;
      *) log "reapply skip reason=bad-installer installer=$installer"; continue ;;
    esac
    [ -n "$marker" ] && [ -n "$old_gen" ] && [ -f "$REPO/$installer" ] || {
      log "reapply skip reason=incomplete-record path=$rec"
      continue
    }
    current="$(ccc_installer_gen_stamp_auto "$REPO/$installer" 2>/dev/null)" || current=""
    [ -n "$current" ] || { log "reapply skip reason=stamp-failed installer=$installer"; continue; }
    if [ "$current" = "$old_gen" ]; then
      log "reapply skip reason=current installer=$installer gen=$current"
      continue
    fi
    if [ -z "$CRONTAB_SNAP" ]; then
      CRONTAB_SNAP="$INSTALL_SNAPSHOT_DIR/crontab.before-reapply"
      "$CRONTAB_CMD" -l >"$CRONTAB_SNAP" 2>/dev/null || : >"$CRONTAB_SNAP"
    fi
    mapfile -t rec_argv < <(jq -r '.argv[]' "$rec" 2>/dev/null) || rec_argv=()
    log "reapply begin installer=$installer old=$old_gen new=$current"
    if ! CCC_CRONTAB_CMD="$CRONTAB_CMD" bash "$REPO/$installer" "${rec_argv[@]}" >>"$LOG" 2>&1; then
      "$CRONTAB_CMD" "$CRONTAB_SNAP" >>"$LOG" 2>&1 || true
      audit "reapply-failed" "$OLD_SHA" "$NEW_SHA" "$CHANGED" "$SETUP_OK" '[]'
      notify "self-update $(git -C "$REPO" rev-parse --short HEAD): cron 재적용 실패 ($installer) — crontab 복원됨. ~/.claude/state/self-update.log" "reapply-fail-$NEW_SHA"
      say "self-update: installer re-apply failed ($installer); crontab restored" >&2
      exit 12
    fi
    if ! "$CRONTAB_CMD" -l 2>/dev/null | grep -F "$marker" | grep -qF "gen=$current"; then
      "$CRONTAB_CMD" "$CRONTAB_SNAP" >>"$LOG" 2>&1 || true
      audit "reapply-verify-failed" "$OLD_SHA" "$NEW_SHA" "$CHANGED" "$SETUP_OK" '[]'
      notify "self-update $(git -C "$REPO" rev-parse --short HEAD): cron 재적용 검증 실패 ($installer) — crontab 복원됨. ~/.claude/state/self-update.log" "reapply-verify-$NEW_SHA"
      say "self-update: installer re-apply did not stamp $marker with $current; crontab restored" >&2
      exit 12
    fi
    REAPPLY_COUNT=$((REAPPLY_COUNT + 1))
    log "reapply ok installer=$installer old=$old_gen new=$current"
  done
  [ "$REAPPLY_COUNT" -gt 0 ] && REAPPLY_NOTE=", cron 재적용 ${REAPPLY_COUNT}건"
fi

# --- restart allowlisted services ----------------------------------------------
SERVICES_JSON='[]'
FAILED=0
RESTARTED=0
if [ -f "$SERVICES_FILE" ]; then
  while IFS= read -r svc; do
    svc="${svc%%#*}"; svc="$(printf '%s' "$svc" | tr -d '[:space:]')"
    [ -n "$svc" ] || continue
    scope=system
    case "$svc" in
      user:*) scope=user; svc="${svc#user:}" ;;
      system:*) svc="${svc#system:}" ;;
    esac
    if [ -z "$svc" ] || ! printf '%s' "$svc" | grep -Eq '^[A-Za-z0-9@._:-]+$'; then
      log "service skipped reason=invalid-name name=$svc"
      continue
    fi
    ok=false
    attempt=0
    while [ "$attempt" -lt 2 ]; do
      attempt=$((attempt + 1))
      systemctl_scope_args=()
      [ "$scope" = user ] && systemctl_scope_args+=(--user)
      if "$SYSTEMCTL" "${systemctl_scope_args[@]}" restart "$svc" >>"$LOG" 2>&1; then
        ok=true
        i=0
        until "$SYSTEMCTL" "${systemctl_scope_args[@]}" is-active --quiet "$svc" 2>/dev/null; do
          i=$((i + 1)); [ "$i" -ge 10 ] && { ok=false; break; }
          sleep 1
        done
      fi
      [ "$ok" = "true" ] && break
      [ "$attempt" -lt 2 ] && log "service retry name=$svc attempt=$attempt scope=$scope"
    done
    [ "$ok" = "true" ] && RESTARTED=$((RESTARTED + 1)) || FAILED=$((FAILED + 1))
    SERVICES_JSON="$(printf '%s' "$SERVICES_JSON" | jq -c --arg n "$svc" --arg s "$scope" --argjson ok "$ok" '. + [{name:$n, ok:$ok, scope:$s}]')"
    log "service name=$svc ok=$ok scope=$scope"
  done < "$SERVICES_FILE"
else
  log "restart skipped reason=no-services-file path=$SERVICES_FILE"
fi

SHORT_NEW="$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null)"
if [ "$FAILED" -gt 0 ]; then
  # Half-apply: the harness is on NEW_SHA but a service did not come back.
  # Rolling the fleet back automatically is an operator policy decision, not
  # this script's to make, so keep the recovery snapshot and name it — the
  # previous code deleted it before the restarts ran, leaving nothing to
  # recover from.
  KEEP_INSTALL_SNAPSHOT=1
  audit "restart-failures" "$OLD_SHA" "$NEW_SHA" "$CHANGED" "$SETUP_OK" "$SERVICES_JSON"
  log "recovery snapshot=$INSTALL_SNAPSHOT_DIR oldSha=$OLD_SHA reason=restart-failure"
  notify "self-update ${SHORT_NEW}: 서비스 ${FAILED}개 재시작 실패 (${RESTARTED}개 성공, 재시도 후). 롤백 자료 보존: ${INSTALL_SNAPSHOT_DIR}. ~/.claude/state/self-update.log 확인 필요." "fail-$NEW_SHA"
  say "self-update: updated to $SHORT_NEW but $FAILED service(s) failed to restart; recovery snapshot retained at $INSTALL_SNAPSHOT_DIR" >&2
  exit 7
fi
# Per #910: code changed but NO service was restarted (services allowlist file
# missing or empty). With an operator-configured external restart command
# (#971, e.g. Termux start.sh), run it HERE — inside the audit/notify boundary
# — instead of letting a hand-chained cron line discard its failure. Success
# falls through to the shared snapshot-cleanup/ok path; failure keeps the
# recovery snapshot, notifies, and exits non-zero. Without a configured
# command, report degraded (not ok) and exit non-zero so it cannot read as
# success.
if [ "$CHANGED" = "true" ] && [ "$RESTARTED" -eq 0 ]; then
  if resolve_restart_cmd >/dev/null 2>&1; then
    if run_external_restart; then
      RESTARTED=1
      SERVICES_JSON="$(printf '%s' "$SERVICES_JSON" | jq -c '. + [{"name":"external-restart","ok":true,"scope":"external"}]')"
      log "external-restart ok; proceeding to cleanup"
    else
      KEEP_INSTALL_SNAPSHOT=1
      SERVICES_JSON="$(printf '%s' "$SERVICES_JSON" | jq -c '. + [{"name":"external-restart","ok":false,"scope":"external"}]')"
      audit "restart-failures" "$OLD_SHA" "$NEW_SHA" "$CHANGED" "$SETUP_OK" "$SERVICES_JSON"
      log "recovery snapshot=$INSTALL_SNAPSHOT_DIR oldSha=$OLD_SHA reason=external-restart-failure"
      notify "self-update ${SHORT_NEW}: 코드 갱신 후 외부 재시작 명령이 실패했습니다 — 브리지가 남아있는지 즉시 확인 필요. 롤백 자료 보존: ${INSTALL_SNAPSHOT_DIR}. ~/.claude/state/self-update.log" "fail-$NEW_SHA"
      say "self-update: updated to $SHORT_NEW but the external restart command failed; recovery snapshot retained at $INSTALL_SNAPSHOT_DIR" >&2
      exit 7
    fi
  else
    audit "degraded-no-services" "$OLD_SHA" "$NEW_SHA" "$CHANGED" "$SETUP_OK" "$SERVICES_JSON"
    notify "self-update ${SHORT_NEW}: 코드 갱신됐으나 재시작된 서비스 없음 (허용목록 누락/비어있음 의심). 실행 중 프로세스가 옛 코드일 수 있음 — self-update.services 확인 필요. ~/.claude/state/self-update.log" "degraded-$NEW_SHA"
    say "self-update: degraded — ${OLD_SHA:0:7} → ${SHORT_NEW}, services restarted: 0 (no allowlisted services; runtime may be stale)" >&2
    exit 11
  fi
fi

if ! rm -rf -- "$INSTALL_SNAPSHOT_DIR"; then
  # Do not turn a failed private-snapshot cleanup into a reported success.
  # Keep the path available to the operator (and prevent the EXIT trap from
  # hiding the original failure with an unobserved second attempt).
  KEEP_INSTALL_SNAPSHOT=1
  audit "snapshot-cleanup-failed" "$OLD_SHA" "$NEW_SHA" "$CHANGED" "$SETUP_OK" "$SERVICES_JSON"
  log "recovery snapshot=$INSTALL_SNAPSHOT_DIR oldSha=$OLD_SHA reason=cleanup-failure"
  notify "self-update ${SHORT_NEW}: 서비스 재시작은 완료됐으나 복구 스냅샷 정리에 실패했습니다. 잔존 경로: ${INSTALL_SNAPSHOT_DIR}. ~/.claude/state/self-update.log 확인 필요." "snapshot-cleanup-fail-$NEW_SHA"
  say "self-update: recovery snapshot cleanup failed; retained path: $INSTALL_SNAPSHOT_DIR" >&2
  exit 10
fi
INSTALL_SNAPSHOT_DIR=""

audit "ok" "$OLD_SHA" "$NEW_SHA" "$CHANGED" "$SETUP_OK" "$SERVICES_JSON"
if [ "$CHANGED" = "true" ]; then
  notify "self-update 완료: ${OLD_SHA:0:7} → ${SHORT_NEW}, 서비스 ${RESTARTED}개 재시작${REAPPLY_NOTE}." "ok-$NEW_SHA"
fi
say "self-update: ok (${OLD_SHA:0:7} → ${SHORT_NEW}, services restarted: $RESTARTED${REAPPLY_NOTE})"
exit 0
