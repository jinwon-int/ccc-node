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
#   ~/.claude/self-update.serving-generation-cmd (optional: one read-only
#      command that prints existing bridge health JSON with the full frozen
#      startup identity. It is the only supported way to reconcile a pending
#      activation record on an unchanged tick; without it
#      the serving identity is unknown and stays pending (#1527).)
#   ~/.claude/self-update.no-reapply (optional: operator kill-switch; when this
#      file exists, installer-managed cron is never rewritten. Env override:
#      CCC_SELF_UPDATE_REAPPLY=0. Agent must not write the file.)
#
# Procedure (run):
#   1. take a lock; resolve the repo (env > repo file > script location > ~/ccc-node)
#   2. preconditions: .git present, clean working tree, on the expected branch.
#      A wrong-branch stall self-recovers when the tree is clean (#1397 C:
#      unpushed history is pinned under refs/ccc-stray/ first); it stays
#      fail-closed on a dirty tree. Historically it recovered ONLY when fully
#      pushed (origin has it at the exact local SHA) and the tree is clean —
#      switching back then cannot lose anything (#1328). Every other
#      wrong-branch shape stays fail-closed and notifies.
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
#      CCC_SELF_UPDATE_AUTO_RECOVER (default 1; 0 restores the unconditional
#      wrong-branch fail-closed abort, #1328),
#      CCC_SELF_UPDATE_SYSTEMCTL (default systemctl; tests inject a fake),
#      CCC_SELF_UPDATE_RESTART_COMMAND_TIMEOUT_SECONDS (180; integer 1..900),
#      CCC_SELF_UPDATE_RESTART_WAIT_SECONDS (60; separate health-probe budget),
#      CCC_STATE_DIR, CCC_PUSH_SPOOL, CCC_NODE,
#      CCC_SELF_UPDATE_FLOCK (flock(1) probe for a foreign regular-file lock, #1945).
#      Pending-activation evaluation (#1527): CCC_SELF_UPDATE_SERVING_GENERATION_CMD
#      (env override for the operator's serving-generation probe file) and
#      CCC_SELF_UPDATE_SERVING_GEN_FILE (file-path override).
# Idle gate: before touching anything the run defers (exit 8) while the telegram
#      bridge is serving a request, so a restart cannot SIGTERM-kill an in-flight
#      `claude` child (exit 143) mid-task. Reads the bridge's health.json.
#      CCC_SELF_UPDATE_HEALTH_FILE (default ~/.telegram_bot/health.json),
#      CCC_SELF_UPDATE_MATRIX_HEALTH_FILE (default ~/.ccc-matrix/health.json;
#      consulted only when ccc-matrix-bridge is in the services allowlist),
#      CCC_SELF_UPDATE_HEALTH_FRESH_SECONDS (90), CCC_SELF_UPDATE_BUSY_MAX_SECONDS
#      (1800 — never defer a task older than this), CCC_SELF_UPDATE_MAX_DEFER_SECONDS
#      (3600 — cap total deferral so continuous load can't starve updates).
#      Fail-open (missing/unreadable/stale health → proceed); --force bypasses.
# Exit: 0 = up-to-date or updated cleanly; 7 = a restart (allowlisted service
#      or external restart-cmd) or a recovery attempt failed; 8 = deferred
#      (bridge busy); 11 = degraded (code updated but nothing restarted and no
#      restart-cmd configured); 12 = installer re-apply failed (crontab was
#      restored); 14 = activation incomplete (#1527): the installed generation
#      was never verified active — a pending-activation record exists (or its
#      bookkeeping failed) and the tick refuses to report convergence from
#      health alone; other non-zero = aborted (reason logged).
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
RESTART_COMMAND_TIMEOUT_SECONDS="${CCC_SELF_UPDATE_RESTART_COMMAND_TIMEOUT_SECONDS-180}"
BRANCH="${CCC_SELF_UPDATE_BRANCH:-main}"
SYSTEMCTL="${CCC_SELF_UPDATE_SYSTEMCTL:-systemctl}"

# Commit-signature verification of the incoming tip (#1591).
#   warn    — verify and report, but still apply (rollout default)
#   enforce — verify and refuse to merge an unverified tip (fail-closed)
#   off     — skip verification entirely (escape hatch)
# The default is deliberately `warn` for the first fleet rollout: this script is
# itself delivered by the mechanism it gates, so an enforce-by-default landing
# would strand every node that cannot verify on the very commit that would fix
# it. Flipping the default to `enforce` is a separate, evidence-gated change.
SIGNATURE_MODE="${CCC_SELF_UPDATE_SIGNATURE_MODE:-warn}"
SIGNATURE_KEYRING="${CCC_SELF_UPDATE_SIGNATURE_KEYRING:-}"

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

# --- lock inspection (#1945) --------------------------------------------------
# The lock contract is a DIRECTORY created with mkdir. An external serializer
# that instead opens the same path with O_CREAT + flock leaves a regular FILE
# behind, and every later mkdir fails with EEXIST forever — on 2026-09-22 that
# silently stalled self-update on 11 fleet nodes (no log line, status said
# "free", and the rmdir-only stale branch could never clear it).
LOCK_FLOCK_CMD="${CCC_SELF_UPDATE_FLOCK:-flock}"

lock_kind() { # -> absent | dir | foreign-file | other
  if [ -L "$LOCK" ]; then echo other
  elif [ -d "$LOCK" ]; then echo dir
  elif [ -f "$LOCK" ]; then echo foreign-file
  elif [ -e "$LOCK" ]; then echo other
  else echo absent
  fi
}

# True when a regular-file lock is provably abandoned. With flock(1) available
# the file is stale only if a non-blocking flock succeeds (nobody holds it);
# conflict or any error is treated as held (fail-closed). Without flock(1) fall
# back to the same 30-minute mtime rule the directory lock uses.
foreign_lock_stale() {
  if command -v "$LOCK_FLOCK_CMD" >/dev/null 2>&1; then
    "$LOCK_FLOCK_CMD" -n -E 75 "$LOCK" true 2>/dev/null
    return $?
  fi
  [ -n "$(find "$LOCK" -maxdepth 0 -mmin +30 2>/dev/null)" ]
}

lock_status() {
  case "$(lock_kind)" in
    dir) echo HELD ;;
    foreign-file)
      if foreign_lock_stale; then echo "FOREIGN-FILE (stale; next run removes it)"
      else echo "FOREIGN-FILE (held by another process)"; fi ;;
    other) echo "BLOCKED (unexpected file type at $LOCK)" ;;
    *) echo free ;;
  esac
}

acquire_lock() {
  mkdir "$LOCK" 2>/dev/null && return 0
  local kind
  kind="$(lock_kind)"
  case "$kind" in
    dir)
      # Stale after 30 minutes.
      if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +30 2>/dev/null)" ]; then
        rmdir "$LOCK" 2>/dev/null
        mkdir "$LOCK" 2>/dev/null && return 0
      fi
      ;;
    foreign-file)
      if foreign_lock_stale; then
        log "lock foreign-file stale; removing mtime=$(date -u -r "$LOCK" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo '?') size=$(wc -c < "$LOCK" 2>/dev/null | tr -d ' ')"
        rm -f -- "$LOCK" 2>/dev/null && mkdir "$LOCK" 2>/dev/null && return 0
      fi
      ;;
  esac
  log "abort reason=lock-held kind=$kind"
  say "self-update: lock held ($kind); aborting" >&2
  return 1
}

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
  # $RANDOM in the name: one run can legitimately emit TWO notifies (e.g. the
  # #1328 wrong-branch recovery notice followed by the completion notice), and
  # a second-resolution timestamp + pid alone made the second file silently
  # overwrite the first.
  fname="$SPOOL/$(printf '%s' "$now" | tr ':' '-')-SelfUpdate-$$_$RANDOM.json"
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

# --- pending-activation evidence (#1527) --------------------------------------
# The installed-SHA marker commits the INSTALLED generation before restarts, so
# a failed activation (e.g. a restart-cmd exiting 6 while the OLD runtime keeps
# serving healthy) used to let the next unchanged tick report convergence from
# "marker == HEAD" plus a passing health probe alone. The small pending-
# activation record below persists the attempt (target generation, outcome,
# bounded evidence) and is cleared ONLY on verified activation: every
# allowlisted restart came back active, an external/recovery restart succeeded
# with its health probe, or the operator-provided serving-generation probe
# supplies existing health JSON with the full frozen startup identity.
# Health alone or a live checkout HEAD probe never clears it. Completed
# receipts remain on disk as outcome=activated to preserve durable evidence.
PENDING_ACTIVATION_FILE="$STATE_DIR/self-update.pending-activation.json"
SERVING_GEN_FILE="${CCC_SELF_UPDATE_SERVING_GEN_FILE:-$CLAUDE_DIR/self-update.serving-generation-cmd}"

ACTIVATION_HELPER="$SELF_UPDATE_DIR/lib/self-update-activation.py"
activation_state() { python3 "$ACTIVATION_HELPER" "$1" "$STATE_DIR" "${@:2}"; }
write_pending_activation() {
  if ! activation_state write "$NEW_SHA" "$OLD_SHA" "$1" "$2" "${3:-$INSTALL_SNAPSHOT_DIR}"; then
    KEEP_INSTALL_SNAPSHOT=1
    log "pending-activation persistence failed; recovery snapshot=$INSTALL_SNAPSHOT_DIR"
    return 1
  fi
}
clear_pending_activation() { activation_state clear "$NEW_SHA"; }
load_pending_activation() {
  local rec rc
  PENDING_TARGET_SHA=""; PENDING_OUTCOME=""; PENDING_SERVICES='[]'
  rec="$(activation_state load "${NEW_SHA:-}")"; rc=$?
  if [ "$rc" != 0 ]; then
    [ "$rc" = 1 ] || log "pending-activation unsafe reason=unsafe-or-interrupted"
    return "$rc"
  fi
  PENDING_TARGET_SHA="$(printf '%s' "$rec" | jq -r .target_sha)"
  PENDING_OUTCOME="$(printf '%s' "$rec" | jq -r .outcome)"
  PENDING_SERVICES="$(printf '%s' "$rec" | jq -c .services)"
}
detect_interrupted_pending_write() { activation_state residue; }
resolve_serving_generation() {
  local cmd
  if [ -n "${CCC_SELF_UPDATE_SERVING_GENERATION_CMD:-}" ]; then
    cmd="$CCC_SELF_UPDATE_SERVING_GENERATION_CMD"
  elif ! cmd="$(read_operator_cmd "$SERVING_GEN_FILE")"; then
    return 1
  fi
  activation_state probe "$REPO" "$cmd" "$RESTART_WAIT_SECONDS"
}
serving_generation_matches() { [ "$1" = "$2" ] && [ "$2" = "$NEW_SHA" ]; }

# Explicitly re-evaluate an incomplete activation on an unchanged tick. This
# NEVER restarts anything by itself (a pending record must not trigger an
# automatic retry or replay, and a healthy old runtime stays untouched) and
# NEVER accepts health-only evidence as convergence. Returns:
#   0 = reconciled against the exact serving generation and cleared
#   2 = still incomplete — caller reports and exits 14
#   3 = runtime is DOWN — caller defers to the existing #971 recovery policy
report_pending_activation() {
  local serving hcmd
  if serving="$(resolve_serving_generation)"; then
    if ! serving_generation_matches "$serving" "$PENDING_TARGET_SHA"; then
      log "pending-activation result=incomplete reason=serving-mismatch target=$PENDING_TARGET_SHA serving=$serving outcome=${PENDING_OUTCOME:-unknown}"
      audit "activation-incomplete" "$OLD_SHA" "$NEW_SHA" false true "$PENDING_SERVICES"
      notify "self-update: ${PENDING_TARGET_SHA:0:7} 세대 활성화가 아직 완료되지 않았습니다 — 서빙 세대($(printf '%.7s' "$serving"))가 설치 목표와 다릅니다. 건강한 구버전 런타임을 임의로 재시작하지 않습니다; 확인 후 수동 개입이 필요합니다. 로그: ~/.claude/state/self-update.log" "pending-$PENDING_TARGET_SHA"
      say "self-update: activation incomplete — serving ${serving:0:7} != installed target ${PENDING_TARGET_SHA:0:7}; not reporting up-to-date" >&2
      return 2
    fi
    if hcmd="$(resolve_health_cmd)" && ! run_bounded_operator_cmd "$RESTART_WAIT_SECONDS" "$hcmd"; then
      log "pending-activation result=unhealthy reason=target-unhealthy target=$PENDING_TARGET_SHA serving=$serving"
      return 3
    fi
    if ! clear_pending_activation; then
      KEEP_INSTALL_SNAPSHOT=1
      log "pending-activation result=clear-failed target=$PENDING_TARGET_SHA"
      audit "activation-clear-failed" "$OLD_SHA" "$NEW_SHA" false true "$PENDING_SERVICES"
      say "self-update: activation verified (${PENDING_TARGET_SHA:0:7} serving) but the pending record could not be cleared; not reporting up-to-date — inspect $PENDING_ACTIVATION_FILE" >&2
      return 2
    fi
    log "pending-activation result=reconciled target=$PENDING_TARGET_SHA serving=$serving"
    audit "activation-reconciled" "$OLD_SHA" "$NEW_SHA" false true "$PENDING_SERVICES"
    notify "self-update: 이전에 실패했던 ${PENDING_TARGET_SHA:0:7} 세대 활성화가 서빙 세대 일치로 확인됐습니다 — 보류 기록을 정리했습니다." "reconciled-$PENDING_TARGET_SHA"
    say "self-update: pending activation reconciled — runtime verified serving ${PENDING_TARGET_SHA:0:7}"
    return 0
  fi
  # Serving identity unknown: health alone never proves activation (#1527).
  if hcmd="$(resolve_health_cmd)" && ! run_bounded_operator_cmd "$RESTART_WAIT_SECONDS" "$hcmd"; then
    log "pending-activation result=unhealthy reason=identity-unknown target=$PENDING_TARGET_SHA outcome=${PENDING_OUTCOME:-unknown}"
    return 3
  fi
  log "pending-activation result=incomplete reason=identity-unknown target=$PENDING_TARGET_SHA outcome=${PENDING_OUTCOME:-unknown}"
  audit "activation-incomplete" "$OLD_SHA" "$NEW_SHA" false true "$PENDING_SERVICES"
  notify "self-update: ${PENDING_TARGET_SHA:0:7} 세대 설치는 완료됐지만 활성화(재시작) 완료 증거가 없습니다(마지막 시도: ${PENDING_OUTCOME:-unknown}). 구버전 런타임이 건강해 보여도 수렴으로 보지 않습니다 — 서빙 세대 확인 또는 self-update.serving-generation-cmd 설정이 필요합니다. 로그: ~/.claude/state/self-update.log" "pending-$PENDING_TARGET_SHA"
  say "self-update: activation incomplete — ${PENDING_TARGET_SHA:0:7} installed but never verified active (outcome=${PENDING_OUTCOME:-unknown}); not reporting up-to-date" >&2
  return 2
}

# Commands run in timeout's process group; kill a TERM-resistant probe after
# one additional second. Never fall back to an unbounded operator command.
run_bounded_operator_cmd() { # <seconds> <command>
  if ! command -v timeout >/dev/null 2>&1; then
    log "operator-command unavailable=timeout"
    return 125
  fi
  timeout --kill-after=1 "$1" bash -c "$2" >>"$LOG" 2>&1
}

# Run the operator's external restart command INSIDE the audit/notify boundary.
# Restart must succeed, then health-cmd must pass within a wall-time budget.
# RESTART_WAIT_SECONDS includes probe execution and retry sleeps; timeout may
# use one extra second to kill TERM-resistant descendants in its process group.
run_external_restart() {
  local rcmd hcmd rc deadline remaining pause started
  rcmd="$(resolve_restart_cmd)" || return 1
  hcmd="$(resolve_health_cmd || true)"
  started=$SECONDS
  log "external-restart begin timeout=${RESTART_COMMAND_TIMEOUT_SECONDS}s"
  run_bounded_operator_cmd "$RESTART_COMMAND_TIMEOUT_SECONDS" "$rcmd"
  rc=$?
  log "external-restart exit=$rc elapsed=$((SECONDS - started))s timeout=${RESTART_COMMAND_TIMEOUT_SECONDS}s"
  [ "$rc" -eq 0 ] || return "$rc"
  if [ -n "$hcmd" ]; then
    started=$SECONDS
    deadline=$((started + RESTART_WAIT_SECONDS))
    while :; do
      remaining=$((deadline - SECONDS))
      [ "$remaining" -gt 0 ] || break
      if run_bounded_operator_cmd "$remaining" "$hcmd"; then
        log "external-restart healthy waited=$((SECONDS - started))s"
        return 0
      fi
      remaining=$((deadline - SECONDS))
      [ "$remaining" -gt 0 ] || break
      pause=3
      [ "$remaining" -ge "$pause" ] || pause=$remaining
      sleep "$pause"
    done
    log "external-restart health-timeout waited=$((SECONDS - started))s"
    return 1
  fi
  return 0
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
    rm -rf -- "${CLAUDE_DIR:?}/$item" || failed=1
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

# Lossless wrong-branch recovery (#1328): switch back to $BRANCH only when
# nothing could possibly be lost. Three conditions, ALL verified; any failure
# keeps the fail-closed abort:
#   1. clean working tree — nothing uncommitted to strand
#   2. origin already has the stray branch at the exact local HEAD — every
#      commit is safe on the remote, so checking out $BRANCH loses nothing
#      (the yukson 2026-08-27 recovery validated exactly this shape)
#   3. local $BRANCH exists and the switch actually lands on it (a linked
#      worktree holding $BRANCH makes git refuse — that state needs a human)
# The stray branch ref itself is deliberately left in place: deleting refs is
# a mutation beyond recovery's mandate.
# #1397 (proposal C): an UNPUSHED stray branch is recoverable too. Switching
# the checkout back to $BRANCH never deletes the stray branch ref, so the
# commits stay reachable; the fail-closed rule existed for the dirty-tree
# case, not for unpushed history. Belt and braces: before switching, the
# stray HEAD is pinned under refs/ccc-stray/<branch>/<utc-ts> so even a later
# `git branch -D` by an operator cannot orphan it. RECOVERY_KIND reports
# which shape was handled (pushed | unpushed) for the notice and the audit.
RECOVERY_KIND=""
recover_stray_branch() { # <stray-branch>
  [ -n "${1:-}" ] || return 1
  [ -z "$(git -C "$REPO" status --porcelain 2>/dev/null)" ] || return 1
  git -C "$REPO" fetch origin >/dev/null 2>&1 || return 1
  local remote_sha head_sha
  head_sha="$(git -C "$REPO" rev-parse HEAD 2>/dev/null)" || return 1
  remote_sha="$(git -C "$REPO" rev-parse --verify --quiet "refs/remotes/origin/$1" 2>/dev/null || true)"
  git -C "$REPO" rev-parse --verify --quiet "refs/heads/$BRANCH" >/dev/null 2>&1 || return 1
  if [ "$remote_sha" = "$head_sha" ]; then
    RECOVERY_KIND="pushed"
  else
    # Unpushed (or partially pushed) history: pin it before touching HEAD.
    git -C "$REPO" update-ref "refs/ccc-stray/$1/$(date -u +%Y%m%dT%H%M%SZ)" "$head_sha" >/dev/null 2>&1 || return 1
    RECOVERY_KIND="unpushed"
  fi
  # #1950: a post-checkout hook's exit status becomes checkout's own, so a
  # switch that fully completed can still exit nonzero (e.g. Termux, where a
  # `#!/usr/bin/env` hook cannot even be exec()d). Decide by the resulting
  # state instead: HEAD must be on $BRANCH with a clean tree. A genuinely
  # refused checkout (worktree-held branch, conflicts) leaves HEAD where it
  # was, so it still fails closed below.
  git -C "$REPO" checkout -q "$BRANCH" >/dev/null 2>&1 || true
  [ "$(git -C "$REPO" symbolic-ref --short HEAD 2>/dev/null)" = "$BRANCH" ] || return 1
  [ -z "$(git -C "$REPO" status --porcelain 2>/dev/null)" ]
}

# Return 0 when the allowlist names <unit> (with or without .service, any scope).
service_allowlisted() {
  local want="${1%.service}" svc
  [ -f "$SERVICES_FILE" ] || return 1
  while IFS= read -r svc; do
    svc="${svc%%#*}"
    svc="$(printf '%s' "$svc" | tr -d '[:space:]')"
    case "$svc" in
      user:*) svc="${svc#user:}" ;;
      system:*) svc="${svc#system:}" ;;
    esac
    [ "${svc%.service}" = "$want" ] && return 0
  done < "$SERVICES_FILE"
  return 1
}

bridge_service_allowlisted() {
  service_allowlisted ccc-telegram-bridge
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
  say "lock: $(lock_status)"
  say "services file: $SERVICES_FILE $([ -f "$SERVICES_FILE" ] && echo "($(grep -cv '^[[:space:]]*\(#\|$\)' "$SERVICES_FILE" 2>/dev/null || true) services)" || echo '(missing)')"
  say "external restart command timeout: ${RESTART_COMMAND_TIMEOUT_SECONDS}s"
  say "post-restart health budget: ${RESTART_WAIT_SECONDS}s"
  pending_rc=0
  load_pending_activation || pending_rc=$?
  case "$pending_rc" in
    0) say "pending activation: INCOMPLETE target=$(printf '%.7s' "$PENDING_TARGET_SHA") outcome=${PENDING_OUTCOME:-unknown}" ;;
    2) say "pending activation: unreadable/unsafe ($PENDING_ACTIVATION_FILE)" ;;
    *) say "pending activation: none" ;;
  esac
  say "-- log (last 5) --"
  tail -5 "$LOG" 2>/dev/null
  exit 0
fi

if [ "$MODE" != "run" ]; then
  say "usage: ccc-self-update.sh [run [--force]|status]" >&2
  exit 2
fi

# Keep timeout/deadline arithmetic finite and reject zero (timeout disables
# its deadline at zero). Status remains available for invalid configuration.
if [[ ! "$RESTART_WAIT_SECONDS" =~ ^[1-9][0-9]{0,4}$ ]] || [ "$RESTART_WAIT_SECONDS" -gt 86400 ]; then
  say "self-update: CCC_SELF_UPDATE_RESTART_WAIT_SECONDS must be an integer in 1..86400" >&2
  exit 2
fi

# A separate command budget covers preflight + drain + candidate readiness.
# Cap at 15 minutes; never disable timeout or extend one restart past the
# lock's 30-minute stale threshold. This is not a deadline for the whole tick.
if [[ ! "$RESTART_COMMAND_TIMEOUT_SECONDS" =~ ^[1-9][0-9]{0,2}$ ]] || [ "$RESTART_COMMAND_TIMEOUT_SECONDS" -gt 900 ]; then
  say "self-update: CCC_SELF_UPDATE_RESTART_COMMAND_TIMEOUT_SECONDS must be an integer in 1..900" >&2
  exit 2
fi

# --- lock (stale after 30 minutes; foreign regular file recovered, #1945) ------
acquire_lock || exit 3
trap cleanup EXIT

# Unsafe or interrupted activation evidence blocks every run, including forced
# and changed ticks, before repository recovery, fetch, setup or restart.
ACTIVATION_PREFLIGHT=0
activation_state load >/dev/null || ACTIVATION_PREFLIGHT=$?
if [ "$ACTIVATION_PREFLIGHT" != 0 ] && [ "$ACTIVATION_PREFLIGHT" != 1 ]; then
  log "pending-activation unsafe reason=unsafe-or-interrupted"
  say "self-update: unsafe activation evidence; refusing mutations" >&2
  exit 14
fi

# --- idle gate: never restart the bridge while it is serving a request --------
# The bridge writes an in-flight workload snapshot to its health.json. Restarting
# it mid-request SIGTERM-kills the in-flight `claude` child (exit 143) and destroys
# the user's work. When the bridge is busy we defer the WHOLE run (nothing fetched
# or restarted) and let the next scheduled tick retry — bounded so a hung/very-long
# request, or continuous load, cannot starve updates forever.
HEALTH_FILE="${CCC_SELF_UPDATE_HEALTH_FILE:-${HOME:-/root}/.telegram_bot/health.json}"
# The Matrix frontend (ccc-matrix-bridge, BOT_DATA_DIR=~/.ccc-matrix) writes the
# same workload snapshot to its own data dir. It is consulted only when that unit
# is allowlisted: this script restarts nothing else, so a busy Matrix turn is
# only at risk when the Matrix unit itself is in the restart set.
MATRIX_HEALTH_FILE="${CCC_SELF_UPDATE_MATRIX_HEALTH_FILE:-${HOME:-/root}/.ccc-matrix/health.json}"
FRESH_SECONDS="${CCC_SELF_UPDATE_HEALTH_FRESH_SECONDS:-90}"
BUSY_MAX_SECONDS="${CCC_SELF_UPDATE_BUSY_MAX_SECONDS:-1800}"
MAX_DEFER_SECONDS="${CCC_SELF_UPDATE_MAX_DEFER_SECONDS:-3600}"
DEFER_MARK="$STATE_DIR/self-update.deferred-since"

# Echo a reason and return 0 when the bridge is busy; return 1 (fail-open) when
# idle, unknown, stale, or over the per-task cap.
health_file_busy() {
  [ -f "$1" ] || return 1
  python3 - "$1" "$FRESH_SECONDS" "$BUSY_MAX_SECONDS" <<'PY'
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

bridge_is_busy() {
  local r
  if r="$(health_file_busy "$HEALTH_FILE")"; then
    printf '%s\n' "$r"
    return 0
  fi
  if service_allowlisted ccc-matrix-bridge && r="$(health_file_busy "$MATRIX_HEALTH_FILE")"; then
    printf 'matrix %s\n' "$r"
    return 0
  fi
  return 1
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
# The optional third argument appends `key=value` detail to the log line only.
# It exists because the abort line recorded the reason but not the offending
# value, so a later reader could see `reason=wrong-branch` without learning
# which branch — the one fact needed to judge whether the stall is a stray
# feature branch or a misconfigured CCC_SELF_UPDATE_BRANCH. Keep it out of the
# notification text, which already spells the value out in prose.
notify_stalled() { # <reason> <text> [log-detail]
  log "abort reason=$1 repo=$REPO${3:+ $3}"
  notify "$2 ~/.claude/state/self-update.log" "stalled-$1"
}

# Verify that <rev> carries a good signature from a pinned trusted key.
#
# The keyring is built in a private, throwaway GNUPGHOME from key material
# vendored in the repo, so the result never depends on (and never mutates) the
# node's own gpg keyring. Trust is pinned by full fingerprint: a GOODSIG alone
# is not enough, because any key the keyring happens to hold would satisfy it.
#
# Prints one of: ok | bad-signature | no-gpg | no-keyring | unverified
verify_commit_signature() { # <rev>
  local rev="$1" keyring gnupghome raw rc=0
  command -v gpg >/dev/null 2>&1 || { printf 'no-gpg'; return 1; }

  keyring="$SIGNATURE_KEYRING"
  [ -n "$keyring" ] || keyring="$SELF_UPDATE_DIR/trusted-keys/github-web-flow.gpg"
  [ -r "$keyring" ] || { printf 'no-keyring'; return 1; }

  gnupghome="$(mktemp -d 2>/dev/null)" || { printf 'no-gpg'; return 1; }
  chmod 700 "$gnupghome" 2>/dev/null || :
  raw="$(GNUPGHOME="$gnupghome" gpg --batch --quiet --import "$keyring" 2>/dev/null \
    && GNUPGHOME="$gnupghome" git -C "$REPO" verify-commit --raw "$rev" 2>&1)" || rc=$?
  rm -rf "$gnupghome" 2>/dev/null || :

  case "$raw" in
    *VALIDSIG*)
      # Pin the fingerprint, not just "some good signature".
      local fpr
      fpr="$(printf '%s\n' "$raw" | sed -n 's/.*VALIDSIG \([A-F0-9]\{40\}\).*/\1/p' | head -1)"
      if printf '%s\n' "$TRUSTED_SIGNING_FPRS" | grep -qxF "$fpr"; then
        printf 'ok'; return 0
      fi
      printf 'unverified'; return 1 ;;
    *BADSIG*) printf 'bad-signature'; return 1 ;;
    *) [ "$rc" -eq 0 ] && { printf 'unverified'; return 1; }
       printf 'unverified'; return 1 ;;
  esac
}

# Full fingerprints permitted to sign the update tip. GitHub signs every
# squash-merge performed through its UI/API with these keys, so a commit pushed
# directly to the branch with a stolen deploy key does NOT carry one.
# NOTE: this proves the commit was created through GitHub, not which human
# authored it; branch protection and CODEOWNERS remain the author control.
TRUSTED_SIGNING_FPRS="${CCC_SELF_UPDATE_TRUSTED_FPRS:-968479A1AFF927E37D1A566BB5690EEEBB952194
5DE3E0509C47EA3CF04A42D34AEE18F83AFDEB23}"
if [ ! -d "$REPO/.git" ]; then
  notify_stalled no-repo "self-update 정지: $REPO 에 git 저장소가 없습니다. 이 노드는 복구 전까지 갱신되지 않습니다."
  say "self-update: no git repo at $REPO (set CCC_SELF_UPDATE_REPO or $REPO_FILE)" >&2
  exit 4
fi
# Checkout owner guard (#1426): on a dual-domain host (gongmyoung — root ssh,
# runtime user owns /opt/ccc-node) a root-context tick fast-forwards the repo as
# root, leaves .git objects root-owned, and deploys hooks into the WRONG home
# while the runtime user's harness silently stays stale. Nothing about that
# self-heals, so it is a terminal abort like no-repo/wrong-branch. Opt out only
# for a deliberately shared checkout: CCC_SELF_UPDATE_ALLOW_OWNER_MISMATCH=1.
REPO_OWNER_UID="$(stat -c %u "$REPO/.git" 2>/dev/null || echo '')"
EUID_NOW="$(id -u 2>/dev/null || echo '')"
if [ -n "$REPO_OWNER_UID" ] && [ -n "$EUID_NOW" ] && [ "$REPO_OWNER_UID" != "$EUID_NOW" ] \
   && [ "${CCC_SELF_UPDATE_ALLOW_OWNER_MISMATCH:-0}" != "1" ]; then
  notify_stalled owner-mismatch "self-update 정지: $REPO 체크아웃 소유자(uid $REPO_OWNER_UID)와 실행 계정(uid $EUID_NOW)이 다릅니다. 소유자 계정으로 실행하거나(예: sudo -u <owner>) 공유 체크아웃이 의도라면 CCC_SELF_UPDATE_ALLOW_OWNER_MISMATCH=1 을 설정하세요." "owner=$REPO_OWNER_UID euid=$EUID_NOW"
  say "self-update: checkout $REPO is owned by uid $REPO_OWNER_UID but running as uid $EUID_NOW; aborting (fail-closed, #1426)" >&2
  exit 4
fi
CUR_BRANCH="$(git -C "$REPO" symbolic-ref --short HEAD 2>/dev/null || echo '?')"
if [ "$CUR_BRANCH" != "$BRANCH" ]; then
  # #1328 proposal 2: a stray branch that is fully pushed with a clean tree is
  # a stall the node can fix itself — switching back cannot lose anything.
  # Anything else (unpushed commits, dirty tree, missing/double-checked-out
  # '$BRANCH') must keep failing closed: auto-mutating around those could
  # destroy the only copy of someone's work. This runs AFTER the bridge-busy
  # gate above, so recovery never swaps the tree under a live session.
  # Opt out with CCC_SELF_UPDATE_AUTO_RECOVER=0.
  if [ "${CCC_SELF_UPDATE_AUTO_RECOVER:-1}" = "1" ] && recover_stray_branch "$CUR_BRANCH"; then
    log "recover reason=wrong-branch from=$CUR_BRANCH kind=$RECOVERY_KIND sha=$(git -C "$REPO" rev-parse HEAD 2>/dev/null)"
    if [ "$RECOVERY_KIND" = "unpushed" ]; then
      notify "self-update: 관리 체크아웃이 '$CUR_BRANCH' 브랜치에 있어 '$BRANCH'로 자동 복구했습니다(clean tree). 이 브랜치에는 원격에 없는 커밋이 있습니다 — 브랜치 ref와 refs/ccc-stray/$CUR_BRANCH/* 에 보존됐으니 worktree에서 이어서 푸시하세요. ~/.claude/state/self-update.log" "wrong-branch-recovered"
      say "self-update: recovered from stray branch '$CUR_BRANCH' (unpushed commits preserved on the branch + refs/ccc-stray) — back on '$BRANCH'"
    else
      notify "self-update: 관리 체크아웃이 '$CUR_BRANCH' 브랜치에 있었으나 커밋이 전부 원격에 있어(동일 SHA, clean tree) '$BRANCH'로 자동 복구했습니다. 브랜치 작업은 git worktree로 분리하세요. ~/.claude/state/self-update.log" "wrong-branch-recovered"
      say "self-update: recovered from stray branch '$CUR_BRANCH' (fully pushed, clean tree) — back on '$BRANCH'"
    fi
  else
    notify_stalled wrong-branch "self-update 정지: 레포가 '$CUR_BRANCH' 브랜치에 있습니다 (기대: '$BRANCH'). 이 노드는 복구 전까지 갱신되지 않습니다 — 관리 체크아웃은 '$BRANCH' 고정, 개발은 git worktree로 분리하세요." "branch=$CUR_BRANCH expected=$BRANCH"
    say "self-update: repo is on '$CUR_BRANCH', expected '$BRANCH'; aborting (fail-closed)" >&2
    exit 4
  fi
fi
if [ -n "$(git -C "$REPO" status --porcelain 2>/dev/null)" ]; then
  notify_stalled dirty-tree "self-update 정지: $REPO 작업 트리에 미커밋 변경이 있습니다. 이 노드는 복구 전까지 갱신되지 않습니다."
  say "self-update: working tree not clean; aborting (fail-closed)" >&2
  exit 4
fi

OLD_SHA="$(git -C "$REPO" rev-parse HEAD 2>/dev/null)"

# Managed installations can require an explicitly reconciled generation.
# Check BEFORE fetch/merge: a stale marker is evidence of drift, never authority
# to reset a serving checkout to some historical commit. Missing markers also
# fail closed in this opt-in mode, including --force.
INSTALLED_SHA_FILE="$STATE_DIR/self-update.installed-sha"
INSTALLED_SHA="$(tr -d '[:space:]' 2>/dev/null < "$INSTALLED_SHA_FILE" || :)"
if [ "${CCC_SELF_UPDATE_REQUIRE_MARKER_MATCH:-0}" = "1" ] && [ "$INSTALLED_SHA" != "$OLD_SHA" ]; then
  log "abort reason=installed-marker-mismatch installed=${INSTALLED_SHA:-missing} checkout=$OLD_SHA"
  say "self-update: installed marker does not match checkout; reconcile deployment before retrying" >&2
  exit 4
fi

# ccc-side-effect: self_update.apply
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

# --- verify the incoming tip BEFORE it becomes HEAD (#1591) -------------------
# Order matters: verifying after the merge would already have moved the working
# checkout onto unverified code, and setup.sh runs from that checkout.
if [ "$SIGNATURE_MODE" != "off" ]; then
  INCOMING_SHA="$(git -C "$REPO" rev-parse "origin/$BRANCH" 2>/dev/null)"
  # Verify on EVERY tick, including up-to-date ones (#1597). An up-to-date tick
  # used to short-circuit to `ok` without running gpg at all, and logged a line
  # indistinguishable from a real verification — so a node with no gpg produced
  # the same "signature ok" as a node that actually verified. On this fleet most
  # ticks are up-to-date (40 of 51 successful ticks on one node), which made the
  # log useless as readiness evidence for flipping the default to `enforce`.
  # Verifying anyway costs ~50ms and turns each daily tick into a capability
  # probe: "can this node's gpg + keyring verify the current tip?".
  if [ "$INCOMING_SHA" = "$OLD_SHA" ]; then SIG_CHANGED=no; else SIG_CHANGED=yes; fi
  SIG_RESULT="$(verify_commit_signature "origin/$BRANCH" || :)"
  if [ "$SIG_RESULT" = "ok" ]; then
    log "signature ok rev=${INCOMING_SHA:-?} changed=$SIG_CHANGED mode=$SIGNATURE_MODE"
  elif [ "$SIGNATURE_MODE" = "enforce" ] && [ "$SIG_CHANGED" = yes ]; then
    # Enforce only against an actually-new tip. Refusing an up-to-date tick would
    # protect nothing — that code is already checked out and running — while
    # cutting the node off from the update that would fix it.
    notify_stalled unverified-signature \
      "self-update 정지: origin/$BRANCH 최신 커밋의 서명을 신뢰할 수 없습니다 ($SIG_RESULT). 이 노드는 복구 전까지 갱신되지 않습니다." \
      "rev=${INCOMING_SHA:-?} result=$SIG_RESULT"
    say "self-update: refusing unverified tip ${INCOMING_SHA:-?} ($SIG_RESULT); aborting (fail-closed)" >&2
    exit 13
  else
    log "signature $SIG_RESULT rev=${INCOMING_SHA:-?} changed=$SIG_CHANGED mode=$SIGNATURE_MODE proceeding"
    say "self-update: WARNING unverified tip ${INCOMING_SHA:-?} ($SIG_RESULT, changed=$SIG_CHANGED); proceeding because mode=$SIGNATURE_MODE" >&2
  fi
fi

if ! git -C "$REPO" merge --ff-only "origin/$BRANCH" >/dev/null 2>&1; then
  notify_stalled non-ff "self-update 정지: 로컬 브랜치가 origin/$BRANCH 와 분기했습니다 (non-ff). 이 노드는 복구 전까지 갱신되지 않습니다."
  say "self-update: local branch diverged from origin/$BRANCH (non-ff); aborting (fail-closed)" >&2
  exit 5
fi
NEW_SHA="$(git -C "$REPO" rev-parse HEAD 2>/dev/null)"

CHANGED=false
[ "$OLD_SHA" != "$NEW_SHA" ] && CHANGED=true

# Installed-SHA marker (#1422): the checkout SHA alone cannot tell whether
# setup.sh ever deployed it. When another agent `git pull`s the managed
# checkout by hand, HEAD already equals origin, so the pre-#1422 tick said
# "up-to-date" forever while ~/.claude/hooks stayed at the older commit.
# The marker records the last SHA setup.sh installed; a lagging marker turns
# an "up-to-date" tick into a normal redeploy. OLD_SHA remains the actual
# pre-run checkout: artifact rollback restores the actual pre-run snapshot,
# not artifacts from the historical installed marker. A missing marker
# (first tick after this change) adopts HEAD silently — doctor's per-file
# drift rows still cover that one-off case — instead of a fleet-wide redeploy.
INSTALLED_SHA_FILE="$STATE_DIR/self-update.installed-sha"
INSTALLED_SHA="$(tr -d '[:space:]' 2>/dev/null < "$INSTALLED_SHA_FILE" || :)"
if [ "$CHANGED" = "false" ] && [ -n "$INSTALLED_SHA" ] && [ "$INSTALLED_SHA" != "$NEW_SHA" ]; then
  log "install-drift installed=$INSTALLED_SHA checkout=$NEW_SHA reason=checkout-advanced-without-setup"
  CHANGED=true
elif [ "$CHANGED" = "false" ] && [ -z "$INSTALLED_SHA" ] && [ "$FORCE" != "1" ]; then
  # A failed first installation may be unable to create even its activation
  # intent. Its retained recovery snapshot still proves this is not a clean
  # legacy bootstrap. Never turn that failure into a successful HEAD adoption.
  for recovery_evidence in "$STATE_DIR"/self-update-install-rollback.*; do
    if [ -e "$recovery_evidence" ] || [ -L "$recovery_evidence" ]; then
      log "pending-activation result=incomplete reason=markerless-recovery-evidence"
      say "self-update: missing installed marker with recovery evidence; refusing to report up-to-date" >&2
      exit 14
    fi
  done
  # Only an ordinary no-change tick without recovery evidence adopts HEAD. A
  # forced first deployment waits for setup and its config preflight.
  printf '%s\n' "$NEW_SHA" > "$INSTALLED_SHA_FILE" 2>/dev/null || log "warn installed-sha marker write failed path=$INSTALLED_SHA_FILE"
fi

if [ "$CHANGED" = "false" ] && [ "$FORCE" != "1" ]; then
  # Pending-activation evidence (#1527) outranks every up-to-date shortcut: an
  # installed-but-unverified generation must be reported, never papered over by
  # "marker == HEAD" plus a healthy OLD runtime. The report path itself never
  # restarts or retries anything.
  PENDING_STATE=0
  PENDING_HEALTH_FAILED=0
  load_pending_activation || PENDING_STATE=$?
  if [ "$PENDING_STATE" = "2" ]; then
    log "pending-activation result=unsafe refusing-up-to-date target=${PENDING_TARGET_SHA:-none}"
    audit "activation-incomplete" "$OLD_SHA" "$NEW_SHA" false true "[]"
    notify "self-update: 활성화 시도 기록(~/.claude/state/self-update.pending-activation.json)이 손상됐거나 안전하지 않습니다. 확인 전까지 이 노드를 최신 상태로 보고하지 않습니다. 로그: ~/.claude/state/self-update.log" "pending-unsafe"
    say "self-update: pending-activation record is corrupt or unsafe; refusing to report up-to-date (inspect $PENDING_ACTIVATION_FILE)" >&2
    exit 14
  fi
  if [ "$PENDING_STATE" = "0" ]; then
    report_pending_activation
    pending_report_rc=$?
    if [ "$pending_report_rc" = "0" ]; then exit 0; fi
    if [ "$pending_report_rc" = "2" ]; then exit 14; fi
    # rc 3: the runtime is DOWN. That is the pre-existing #971 recovery shape
    # (an updated-but-down node), not a healthy-old-runtime masquerade, so the
    # default recovery policy below still applies. PENDING_HEALTH_FAILED keeps
    # that path honest when no recovery restart target is configured.
    PENDING_HEALTH_FAILED=1
  fi
  if detect_interrupted_pending_write; then
    audit "activation-incomplete" "$OLD_SHA" "$NEW_SHA" false true "[]"
    notify "self-update: 활성화 기록 저장이 중단된 흔적이 있습니다 — 마지막 갱신이 재시작 전에 끊겼을 수 있습니다. 서빙 세대 확인이 필요합니다. 로그: ~/.claude/state/self-update.log" "pending-interrupted-$NEW_SHA"
    say "self-update: interrupted pending-activation write detected; refusing to report up-to-date" >&2
    exit 14
  fi
  # Second-slot runtime recovery (#971): code is current, but an earlier
  # chained restart may have failed and left the runtime down. When the
  # operator configured both a health probe and an external restart command,
  # verify runtime health and attempt ONE recovery restart — with the outcome
  # audited and notified, never discarded.
  if hcmd="$(resolve_health_cmd)" && resolve_restart_cmd >/dev/null 2>&1; then
    if run_bounded_operator_cmd "$RESTART_WAIT_SECONDS" "$hcmd"; then
      log "done result=up-to-date sha=$NEW_SHA runtime=healthy"
      say "self-update: already up to date ($(git -C "$REPO" rev-parse --short HEAD))"
      exit 0
    fi
    SHORT_CUR="$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null)"
    log "runtime unhealthy at up-to-date tick; attempting recovery restart"
    if run_external_restart; then
      # A verified successful restart IS activation of the installed
      # generation: a pending record may finally be cleared (no-op if absent).
      if ! clear_pending_activation; then
        KEEP_INSTALL_SNAPSHOT=1
        audit "activation-clear-failed" "$OLD_SHA" "$NEW_SHA" false true "[]"
        notify "self-update ${SHORT_CUR}: 복구 재시작은 성공했지만 활성화 기록 정리에 실패했습니다. 보존된 활성화 기록과 복구 자료를 유지하고 운영자가 원인을 확인해 조정해야 합니다." "pending-clear-fail-$NEW_SHA"
        say "self-update: recovery restart succeeded but the pending-activation record could not be cleared" >&2
        exit 14
      fi
      audit "runtime-recovered" "$OLD_SHA" "$NEW_SHA" "$CHANGED" true '[{"name":"external-restart","ok":true,"scope":"external"}]'
      notify "self-update ${SHORT_CUR}: 코드는 최신이나 런타임 다운 감지 — 외부 재시작으로 복구 완료. ~/.claude/state/self-update.log" "recovered-$NEW_SHA"
      say "self-update: code up to date but runtime was down; recovered via external restart"
      exit 0
    fi
    if [ "$PENDING_STATE" = "0" ]; then
      # Refine the retained evidence with the failed recovery attempt.
      write_pending_activation "recovery-restart-failed" '[{"name":"external-restart","ok":false,"scope":"external"}]' "" || exit 14
    fi
    audit "runtime-down" "$OLD_SHA" "$NEW_SHA" "$CHANGED" true '[{"name":"external-restart","ok":false,"scope":"external"}]'
    notify "self-update ${SHORT_CUR} 경고: 코드는 최신이나 런타임이 다운 상태이며 복구 재시작도 실패했습니다. 브리지가 남아있는지 즉시 확인 필요. ~/.claude/state/self-update.log" "runtime-down-$NEW_SHA"
    say "self-update: code up to date but runtime is DOWN and the recovery restart failed" >&2
    exit 7
  fi
  if [ "${PENDING_HEALTH_FAILED:-0}" = "1" ]; then
    audit "activation-incomplete" "$OLD_SHA" "$NEW_SHA" false true "$PENDING_SERVICES"
    notify "self-update: ${PENDING_TARGET_SHA:0:7} 세대 활성화가 확인되지 않은 상태에서 런타임이 건강하지 않고 복구 재시작 대상도 없습니다. 수동 확인 필요. 로그: ~/.claude/state/self-update.log" "pending-$PENDING_TARGET_SHA"
    say "self-update: activation incomplete and runtime unhealthy with no recovery restart configured; not reporting up-to-date" >&2
    exit 14
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
# Commit the installed generation only after setup AND its rollback-capable
# config preflight succeed. Otherwise rollback leaves a rejected NEW_SHA in
# the marker and a later hand-pulled checkout can incorrectly skip redeploy.
# Keep this before restarts: a runtime failure does not undo installed assets.
# Durable pending evidence precedes advancing the installed marker. On any
# persistence uncertainty stop before restart and keep the recovery snapshot.
if ! write_pending_activation "pending" '[]' "$INSTALL_SNAPSHOT_DIR"; then
  say "self-update: activation evidence could not be persisted; recovery snapshot retained" >&2
  exit 14
fi
if ! printf '%s\n' "$NEW_SHA" > "$INSTALLED_SHA_FILE"; then
  KEEP_INSTALL_SNAPSHOT=1
  say "self-update: installed marker publication failed; activation remains pending" >&2
  exit 14
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
      KEEP_INSTALL_SNAPSHOT=1
      write_pending_activation "reapply-aborted" '[]' "$INSTALL_SNAPSHOT_DIR" || exit 14
      audit "reapply-failed" "$OLD_SHA" "$NEW_SHA" "$CHANGED" "$SETUP_OK" '[]'
      notify "self-update $(git -C "$REPO" rev-parse --short HEAD): cron 재적용 실패 ($installer) — crontab 복원됨. ~/.claude/state/self-update.log" "reapply-fail-$NEW_SHA"
      say "self-update: installer re-apply failed ($installer); crontab restored" >&2
      exit 12
    fi
    if ! "$CRONTAB_CMD" -l 2>/dev/null | grep -F "$marker" | grep -qF "gen=$current"; then
      "$CRONTAB_CMD" "$CRONTAB_SNAP" >>"$LOG" 2>&1 || true
      KEEP_INSTALL_SNAPSHOT=1
      write_pending_activation "reapply-verify-aborted" '[]' "$INSTALL_SNAPSHOT_DIR" || exit 14
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
  write_pending_activation "restart-failed" "$SERVICES_JSON" "$INSTALL_SNAPSHOT_DIR" || exit 14
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
if { [ "$CHANGED" = "true" ] || [ "$FORCE" = "1" ]; } && [ "$RESTARTED" -eq 0 ]; then
  if resolve_restart_cmd >/dev/null 2>&1; then
    if run_external_restart; then
      RESTARTED=1
      SERVICES_JSON="$(printf '%s' "$SERVICES_JSON" | jq -c '. + [{"name":"external-restart","ok":true,"scope":"external"}]')"
      log "external-restart ok; proceeding to cleanup"
    else
      KEEP_INSTALL_SNAPSHOT=1
      SERVICES_JSON="$(printf '%s' "$SERVICES_JSON" | jq -c '. + [{"name":"external-restart","ok":false,"scope":"external"}]')"
      write_pending_activation "external-restart-failed" "$SERVICES_JSON" "$INSTALL_SNAPSHOT_DIR" || exit 14
      audit "restart-failures" "$OLD_SHA" "$NEW_SHA" "$CHANGED" "$SETUP_OK" "$SERVICES_JSON"
      log "recovery snapshot=$INSTALL_SNAPSHOT_DIR oldSha=$OLD_SHA reason=external-restart-failure"
      notify "self-update ${SHORT_NEW}: 코드 갱신 후 외부 재시작 명령이 실패했습니다 — 브리지가 남아있는지 즉시 확인 필요. 롤백 자료 보존: ${INSTALL_SNAPSHOT_DIR}. ~/.claude/state/self-update.log" "fail-$NEW_SHA"
      say "self-update: updated to $SHORT_NEW but the external restart command failed; recovery snapshot retained at $INSTALL_SNAPSHOT_DIR" >&2
      exit 7
    fi
  else
    KEEP_INSTALL_SNAPSHOT=1
    write_pending_activation "degraded-no-restart-target" "$SERVICES_JSON" "$INSTALL_SNAPSHOT_DIR" || exit 14
    audit "degraded-no-services" "$OLD_SHA" "$NEW_SHA" "$CHANGED" "$SETUP_OK" "$SERVICES_JSON"
    notify "self-update ${SHORT_NEW}: 코드 갱신됐으나 재시작된 서비스 없음 (허용목록 누락/비어있음 의심). 실행 중 프로세스가 옛 코드일 수 있음 — self-update.services 확인 필요. ~/.claude/state/self-update.log" "degraded-$NEW_SHA"
    say "self-update: degraded — ${OLD_SHA:0:7} → ${SHORT_NEW}, services restarted: 0 (no allowlisted services; runtime may be stale)" >&2
    exit 11
  fi
fi

# Verified activation (#1527): every allowlisted restart came back active, or
# the operator's external restart command and its health probe passed. The
# attempt record was evidence until proven; only now may it be cleared. A
# failed clear is fail-closed — the next unchanged tick would otherwise
# re-report an activation that actually completed.
if ! clear_pending_activation; then
  KEEP_INSTALL_SNAPSHOT=1
  audit "activation-clear-failed" "$OLD_SHA" "$NEW_SHA" "$CHANGED" "$SETUP_OK" "$SERVICES_JSON"
  notify "self-update ${SHORT_NEW}: 활성화는 완료됐지만 시도 기록 정리에 실패했습니다. 보존된 활성화 기록과 복구 자료를 유지하고 운영자가 원인을 확인해 조정해야 합니다." "pending-clear-fail-$NEW_SHA"
  say "self-update: activation completed but the pending-activation record could not be cleared; not reporting clean success" >&2
  exit 14
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
