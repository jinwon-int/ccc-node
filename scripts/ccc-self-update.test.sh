#!/usr/bin/env bash
# Tests for ccc-self-update.sh — hermetic: fixture git repos + fake systemctl.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
SELFUP="$HERE/ccc-self-update.sh"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$HERE/../claude/hooks/lib/test-stub.sh"
# Fixtures supply every CCC_* input this suite needs; ambient harness variables
# from a live node must not reach them (#1023).
ccc_test_reset_hook_env
pass=0; fail=0
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT

ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t

# Keep fixtures hermetic when the operator shell exports live self-update
# settings. In particular, a real busy health file must not defer every fixture
# update before the tests install their own health file in section 7. Unsetting
# CCC_SELF_UPDATE_HEALTH_FILE is NOT enough: the script then falls back to the
# node's real ~/.telegram_bot/health.json, and on a node whose bridge is
# actively serving a session every fixture update defers (rc=8) and the suite
# mass-fails. Point it at a nonexistent fixture path instead (fail-open).
# CODEX_HOME must likewise never resolve to the operator's real ~/.codex: the
# artifact snapshot/rollback covers the Codex policy config (#1131), so an
# unset CODEX_HOME would let a FAILED-fixture restore delete the live file.
unset CCC_SELF_UPDATE_BRANCH CCC_SELF_UPDATE_SERVICES
unset CCC_SELF_UPDATE_RESTART_CMD CCC_SELF_UPDATE_HEALTH_CMD
unset CCC_SELF_UPDATE_RESTART_CMD_FILE CCC_SELF_UPDATE_HEALTH_CMD_FILE
unset CCC_SELF_UPDATE_HEALTH_FRESH_SECONDS
unset CCC_SELF_UPDATE_BUSY_MAX_SECONDS CCC_SELF_UPDATE_MAX_DEFER_SECONDS
unset CCC_SELF_UPDATE_REAPPLY CCC_SELF_UPDATE_CRONTAB_CMD
unset CODEX_HOME
export CCC_SELF_UPDATE_HEALTH_FILE="$TMP/no-such-health.json"

# Fixture: origin repo with a stub setup.sh, plus a node-side clone.
ORIGIN="$TMP/origin.git"
REPO="$TMP/node/ccc-node"
git init -q --bare "$ORIGIN"
git init -q -b main "$TMP/seed"
cat > "$TMP/seed/setup.sh" <<'SH'
#!/usr/bin/env bash
echo "setup ran at $(git rev-parse --short HEAD)" >> "${SETUP_MARKER:?}"
SH
printf '%s\n' 'bridge/.env' > "$TMP/seed/.gitignore"
git -C "$TMP/seed" add -A && git -C "$TMP/seed" commit -qm one
git -C "$TMP/seed" remote add origin "$ORIGIN" && git -C "$TMP/seed" push -q origin main
git -C "$ORIGIN" symbolic-ref HEAD refs/heads/main
mkdir -p "$(dirname "$REPO")"
git clone -q "$ORIGIN" "$REPO"

# Fake systemctl records calls; fails units containing "bad".
FAKEBIN="$TMP/bin"; mkdir -p "$FAKEBIN"
cat > "$FAKEBIN/fakesystemctl" <<SH
#!/usr/bin/env bash
echo "\$*" >> "$TMP/systemctl.calls"
case "\$*" in
  *flaky*)
    if [ ! -e "$TMP/flaky.failed" ]; then
      : > "$TMP/flaky.failed"
      exit 1
    fi
    ;;
  *bad*) exit 1 ;;
esac
exit 0
SH
chmod +x "$FAKEBIN/fakesystemctl"

export FAKE_CRON="$TMP/crontab.txt"
: > "$FAKE_CRON"
cat > "$FAKEBIN/crontab" <<'STUB'
#!/usr/bin/env bash
f="${FAKE_CRON:?}"
case "${1:-}" in
  -l) [ -s "$f" ] && cat "$f" || exit 1 ;;
  -)  cat > "$f" ;;
  *)  [ -n "${1:-}" ] && [ -f "$1" ] && cat "$1" > "$f" ;;
esac
STUB
chmod +x "$FAKEBIN/crontab"

CLAUDE="$TMP/claude"
STATE="$CLAUDE/state"
HERMES="$TMP/hermes"
mkdir -p "$STATE" "$CLAUDE" "$HERMES" "$TMP/codex"
export SETUP_MARKER="$TMP/setup.marker"

run_selfup() {
  CCC_CLAUDE_DIR="$CLAUDE" CCC_STATE_DIR="$STATE" CCC_PUSH_SPOOL="$TMP/spool" \
  CCC_HERMES_DIR="$HERMES" CODEX_HOME="${CODEX_HOME:-$TMP/codex}" \
  CCC_SELF_UPDATE_BRIDGE_PROJECT_ROOT="$TMP/project" \
  CCC_SELF_UPDATE_REPO="${REPO_OVERRIDE:-$REPO}" CCC_SELF_UPDATE_SYSTEMCTL="$FAKEBIN/fakesystemctl" \
  CCC_SELF_UPDATE_CRONTAB_CMD="$FAKEBIN/crontab" CCC_CRONTAB_CMD="$FAKEBIN/crontab" \
  FAKE_CRON="$FAKE_CRON" PATH="$FAKEBIN:$PATH" \
  CCC_SELF_UPDATE_RESTART_WAIT_SECONDS=3 \
  CCC_NODE=testnode bash "$SELFUP" "$@"
}

# --- 1) up-to-date: no setup, no restarts -------------------------------------
out="$(run_selfup run)"; rc=$?
ok "up-to-date exits 0" '[ "$rc" = 0 ] && grep -q "already up to date" <<<"$out"'
ok "up-to-date does not run setup" '[ ! -f "$SETUP_MARKER" ]'

# --- 2) new commit on origin: pull + setup + allowlisted restarts -------------
echo change > "$TMP/seed/file.txt"
git -C "$TMP/seed" add -A && git -C "$TMP/seed" commit -qm two && git -C "$TMP/seed" push -q origin main
printf '%s\n' 'hermes-broker' '# comment line' 'a2a-worker' > "$CLAUDE/self-update.services"
out="$(run_selfup run)"; rc=$?
ok "update exits 0" '[ "$rc" = 0 ] && grep -q "services restarted: 2" <<<"$out"'
ok "repo fast-forwarded" '[ "$(git -C "$REPO" rev-parse HEAD)" = "$(git -C "$TMP/seed" rev-parse HEAD)" ]'
ok "setup.sh ran" '[ -f "$SETUP_MARKER" ]'
ok "only allowlisted services restarted" 'grep -q "restart hermes-broker" "$TMP/systemctl.calls" && grep -q "restart a2a-worker" "$TMP/systemctl.calls" && [ "$(grep -c "^restart " "$TMP/systemctl.calls")" = 2 ]'
ok "system services retain the default system scope in the audit" \
  'grep '"'"'"name":"hermes-broker","ok":true,"scope":"system"'"'"' "$STATE/self-update.log" >/dev/null'
ok "audit record written" 'grep -q "\"result\":\"ok\"" "$STATE/self-update.log"'
ok "owner notification queued" 'ls "$TMP/spool"/*SelfUpdate*.json >/dev/null 2>&1 && jq -r .text "$TMP/spool"/*SelfUpdate*.json | grep -q "self-update 완료"'
ok "successful update removes private recovery snapshot" \
  '! compgen -G "$STATE/self-update-install-rollback.*" >/dev/null'

# --- #910: code changed but NO services allowlist -> degraded (silent drift) --
echo drift > "$TMP/seed/file.txt"
git -C "$TMP/seed" add -A && git -C "$TMP/seed" commit -qm three && git -C "$TMP/seed" push -q origin main
rm -f "$CLAUDE/self-update.services"   # allowlist missing -> nothing restarted
out="$(run_selfup run 2>&1)"; rc=$?
ok "no-services-file on change exits 11 (degraded)" '[ "$rc" = 11 ]'
ok "no-services-file reported as degraded, not ok" 'grep -q "\"result\":\"degraded-no-services\"" "$STATE/self-update.log"'
ok "degraded run still fast-forwards the repo" '[ "$(git -C "$REPO" rev-parse HEAD)" = "$(git -C "$TMP/seed" rev-parse HEAD)" ]'
ok "degraded notification warns of stale runtime" 'grep -rh "재시작된 서비스 없음" "$TMP/spool" >/dev/null 2>&1'
# restore the allowlist so later sections restart normally
printf '%s\n' 'hermes-broker' 'a2a-worker' > "$CLAUDE/self-update.services"

# --- #971: external restart-cmd runs INSIDE the audit/notify boundary ---------
# (1) changed + no allowlist + restart-cmd succeeds -> ok, not degraded.
echo drift2 > "$TMP/seed/file2.txt"
git -C "$TMP/seed" add -A && git -C "$TMP/seed" commit -qm four && git -C "$TMP/seed" push -q origin main
rm -f "$CLAUDE/self-update.services"
printf 'touch %s\n' "$TMP/external-restarted.marker" > "$CLAUDE/self-update.restart-cmd"
out="$(run_selfup run 2>&1)"; rc=$?
ok "external restart-cmd on change exits 0 (not degraded 11)" '[ "$rc" = 0 ]'
ok "external restart-cmd actually ran" '[ -f "$TMP/external-restarted.marker" ]'
ok "external restart audited as ok with external scope" \
  'grep -q "\"result\":\"ok\"" "$STATE/self-update.log" && grep -q "\"name\":\"external-restart\",\"ok\":true" "$STATE/self-update.log"'

# (2) changed + no allowlist + restart-cmd FAILS -> rc 7 + failure notified,
#     never silently discarded (the daegyo cron `exit 0` bug).
echo drift3 > "$TMP/seed/file3.txt"
git -C "$TMP/seed" add -A && git -C "$TMP/seed" commit -qm five && git -C "$TMP/seed" push -q origin main
printf '%s\n' 'exit 1' > "$CLAUDE/self-update.restart-cmd"
out="$(run_selfup run 2>&1)"; rc=$?
ok "failing external restart-cmd exits 7" '[ "$rc" = 7 ]'
ok "failing external restart audited as restart-failures" \
  'grep -q "\"result\":\"restart-failures\"" "$STATE/self-update.log" && grep -q "\"name\":\"external-restart\",\"ok\":false" "$STATE/self-update.log"'
ok "failing external restart notifies immediately" 'grep -rh "외부 재시작 명령이 실패" "$TMP/spool" >/dev/null 2>&1'
ok "external restart failure retains recovery snapshot" 'compgen -G "$STATE/self-update-install-rollback.*" >/dev/null'
rm -rf "$STATE"/self-update-install-rollback.*

# (3) up-to-date but runtime DOWN + health/restart-cmd -> recovery restart.
printf '[ -f %s ]\n' "$TMP/runtime-healthy" > "$CLAUDE/self-update.health-cmd"
printf 'touch %s\n' "$TMP/runtime-healthy" > "$CLAUDE/self-update.restart-cmd"
out="$(run_selfup run 2>&1)"; rc=$?
ok "up-to-date with down runtime recovers (rc 0)" '[ "$rc" = 0 ]'
ok "recovery restart ran and runtime is healthy" '[ -f "$TMP/runtime-healthy" ]'
ok "recovery audited as runtime-recovered" 'grep -q "\"result\":\"runtime-recovered\"" "$STATE/self-update.log"'
ok "recovery notified" 'grep -rh "런타임 다운 감지" "$TMP/spool" >/dev/null 2>&1'

# (4) up-to-date and runtime healthy -> no recovery attempt at all.
printf 'touch %s\n' "$TMP/restart-should-not-run" > "$CLAUDE/self-update.restart-cmd"
out="$(run_selfup run 2>&1)"; rc=$?
ok "healthy up-to-date tick exits 0 without recovery" '[ "$rc" = 0 ] && [ ! -e "$TMP/restart-should-not-run" ]'
ok "healthy tick does not audit a second recovery" '[ "$(grep -c "runtime-recovered" "$STATE/self-update.log")" = 1 ]'

# (5) up-to-date, runtime down, recovery FAILS -> rc 7 + notified.
rm -f "$TMP/runtime-healthy"
printf '[ -f %s ]\n' "$TMP/never-healthy" > "$CLAUDE/self-update.health-cmd"
printf '%s\n' 'exit 1' > "$CLAUDE/self-update.restart-cmd"
out="$(run_selfup run 2>&1)"; rc=$?
ok "failed recovery exits 7" '[ "$rc" = 7 ]'
ok "failed recovery audited as runtime-down" 'grep -q "\"result\":\"runtime-down\"" "$STATE/self-update.log"'
ok "failed recovery notifies" 'grep -rh "복구 재시작도 실패" "$TMP/spool" >/dev/null 2>&1'

# cleanup: restore the allowlist and drop the external-cmd fixtures.
rm -f "$CLAUDE/self-update.restart-cmd" "$CLAUDE/self-update.health-cmd"
printf '%s\n' 'hermes-broker' 'a2a-worker' > "$CLAUDE/self-update.services"

# A target commit must not restart into existing invalid node-local bridge
# timeout settings; it rolls back before systemctl touches the allowlisted unit.
mkdir -p "$TMP/seed/bridge"
cp "$ROOT/bridge/runtime_config_check.py" "$TMP/seed/bridge/runtime_config_check.py"
git -C "$TMP/seed" add bridge/runtime_config_check.py
git -C "$TMP/seed" commit -qm add-bridge-runtime-preflight
git -C "$TMP/seed" push -q origin main
mkdir -p "$REPO/bridge"
printf '%s\n' 'CLAUDE_PROCESS_TIMEOUT=3600' > "$REPO/bridge/.env"
printf '%s\n' 'ccc-telegram-bridge.service' > "$CLAUDE/self-update.services"
OLD_HEAD="$(git -C "$REPO" rev-parse HEAD)"
: > "$TMP/systemctl.calls"
out="$(run_selfup run 2>&1)"; rc=$?
ok "invalid bridge runtime config aborts before restart" \
  '[ "$rc" = 6 ] && grep -q "preflight failed" <<<"$out" && [ ! -s "$TMP/systemctl.calls" ]'
ok "invalid bridge runtime config rolls repository back" \
  '[ "$(git -C "$REPO" rev-parse HEAD)" = "$OLD_HEAD" ] && grep -q "bridge-config-preflight-failed-rolled-back" "$STATE/self-update.log"'

# Repair the node-local setting; the same target commit must now pass the gate.
printf '%s\n' 'CLAUDE_PROCESS_TIMEOUT=3600' \
  'CCC_DELEGATED_TASK_STALL_SECONDS=1800' > "$REPO/bridge/.env"
out="$(run_selfup run 2>&1)"; rc=$?
ok "valid bridge runtime config permits allowlisted restart" \
  '[ "$rc" = 0 ] && grep -q "restart ccc-telegram-bridge.service" "$TMP/systemctl.calls"'

# A user-scoped bridge stays inside the same updater transaction: systemctl
# receives --user for both restart and is-active, and the audit names the scope.
printf '%s\n' 'user:ccc-telegram-bridge.service' > "$CLAUDE/self-update.services"
: > "$TMP/systemctl.calls"
out="$(run_selfup run --force 2>&1)"; rc=$?
ok "user-scoped bridge restart succeeds inside self-update" \
  '[ "$rc" = 0 ] && grep -q "^--user restart ccc-telegram-bridge.service$" "$TMP/systemctl.calls" && grep -q "^--user is-active --quiet ccc-telegram-bridge.service$" "$TMP/systemctl.calls"'
ok "user-scoped bridge restart is audited with its effective unit and scope" \
  'grep '"'"'"name":"ccc-telegram-bridge.service","ok":true,"scope":"user"'"'"' "$STATE/self-update.log" >/dev/null'
ok "user-scoped bridge success removes the recovery snapshot" \
  '! compgen -G "$STATE/self-update-install-rollback.*" >/dev/null'

# A transient restart failure gets exactly one retry. If the retry succeeds,
# the update is successful and its recovery snapshot must not become residue.
printf '%s\n' 'flaky-unit' > "$CLAUDE/self-update.services"
rm -f "$TMP/flaky.failed"
: > "$TMP/systemctl.calls"
out="$(run_selfup run --force 2>&1)"; rc=$?
ok "transient restart failure succeeds on one bounded retry" \
  '[ "$rc" = 0 ] && [ "$(grep -c "^restart flaky-unit$" "$TMP/systemctl.calls")" = 2 ]'
ok "successful retry is recorded" \
  'grep -q "service retry name=flaky-unit attempt=1" "$STATE/self-update.log"'
ok "successful retry removes the recovery snapshot" \
  '! compgen -G "$STATE/self-update-install-rollback.*" >/dev/null'

# A success-path cleanup failure must not be hidden by clearing the snapshot
# variable and continuing to an `ok` audit. Inject an rm that fails only for
# the private recovery directory; all earlier setup/deploy work remains real.
cat > "$FAKEBIN/rm" <<'SH'
#!/usr/bin/env bash
case "$*" in *self-update-install-rollback.*) exit 97 ;; esac
exec /bin/rm "$@"
SH
chmod +x "$FAKEBIN/rm"
rm -f "$TMP/spool"/*.json
out="$(PATH="$FAKEBIN:$PATH" run_selfup run --force 2>&1)"; rc=$?
ok "snapshot cleanup failure exits fail-closed" \
  '[ "$rc" = 10 ] && grep -q "snapshot cleanup failed" <<<"$out"'
ok "snapshot cleanup failure is audited without a false success" \
  'grep -q "\"result\":\"snapshot-cleanup-failed\"" "$STATE/self-update.log" && [ "$(grep "\"result\":" "$STATE/self-update.log" | tail -1 | jq -r .result)" = "snapshot-cleanup-failed" ]'
ok "snapshot cleanup failure retains and reports the recovery path" \
  'compgen -G "$STATE/self-update-install-rollback.*" >/dev/null && grep -q "retained path" <<<"$out" && jq -r .text "$TMP/spool"/*SelfUpdate*.json | grep -q "잔존 경로"'
rm -f "$FAKEBIN/rm"
rm -rf "$STATE"/self-update-install-rollback.*

# Snapshot permission failures must be fail-closed even though the snapshot
# helper is called in an `if ! ...` conditional (where Bash suppresses errexit
# inside the function body).
cat > "$FAKEBIN/chmod" <<'SH'
#!/usr/bin/env bash
case "$*" in *self-update-install-rollback*) exit 98 ;; esac
exec /bin/chmod "$@"
SH
chmod +x "$FAKEBIN/chmod"
setup_count_before="$(wc -l < "$SETUP_MARKER")"
out="$(PATH="$FAKEBIN:$PATH" run_selfup run --force 2>&1)"; rc=$?
ok "snapshot chmod failure is fail-closed before setup" \
  '[ "$rc" = 6 ] && [ "$(wc -l < "$SETUP_MARKER")" = "$setup_count_before" ] && grep -q "artifact-snapshot-failed" "$STATE/self-update.log"'
rm -f "$FAKEBIN/chmod"

ln -s "$TMP/missing-managed-target" "$CLAUDE/settings.json"
setup_count_before="$(wc -l < "$SETUP_MARKER")"
out="$(run_selfup run --force 2>&1)"; rc=$?
ok "managed artifact symlink is rejected before setup" \
  '[ "$rc" = 6 ] && [ "$(wc -l < "$SETUP_MARKER")" = "$setup_count_before" ] && grep -q "artifact-snapshot-failed" "$STATE/self-update.log"'
rm -f "$CLAUDE/settings.json"

# --- 3) service restart failure is reported ------------------------------------
echo change3 > "$TMP/seed/file.txt"
git -C "$TMP/seed" add -A && git -C "$TMP/seed" commit -qm three && git -C "$TMP/seed" push -q origin main
printf '%s\n' 'bad-unit' > "$CLAUDE/self-update.services"
rm -f "$TMP/spool"/*.json
out="$(run_selfup run 2>&1)"; rc=$?
ok "restart failure exits non-zero" '[ "$rc" = 7 ] && grep -q "failed to restart" <<<"$out"'
ok "restart failure audit is explicit and names the degraded service" \
  'grep -q "\"result\":\"restart-failures\"" "$STATE/self-update.log" && grep -q "\"name\":\"bad-unit\",\"ok\":false" "$STATE/self-update.log"'
ok "failure notification queued" 'jq -r .text "$TMP/spool"/*SelfUpdate*.json 2>/dev/null | grep -q "재시작 실패"'
# A restart failure is a half-apply (harness on NEW_SHA, service down), so the
# recovery snapshot must SURVIVE for the operator to roll back from. Before
# #869 it was deleted before the restarts even ran, leaving nothing to recover
# with. Retention is deliberate here and only here; drop it afterwards so the
# later "no residue" invariant still means what it says.
ok "restart failure retains the recovery snapshot for rollback" \
  'compgen -G "$STATE/self-update-install-rollback.*" >/dev/null'
ok "restart failure names the retained snapshot to the operator" \
  'grep -q "recovery snapshot" <<<"$out"'
rm -rf "$STATE"/self-update-install-rollback.*

# --- 4) setup.sh failure rolls back --------------------------------------------
OLD_HEAD="$(git -C "$REPO" rev-parse HEAD)"
mkdir -p "$CLAUDE/hooks"
printf '%s\n' 'old-installed-hook' > "$CLAUDE/hooks/installed-hook.sh"
printf '%s\n' '{"oldHoncho":true}' > "$HERMES/honcho.json"
printf '%s\n' '{"oldLocal":true}' > "$CLAUDE/settings.local.json"
# The broken setup also rewrites the Codex GitHub policy config the way
# ccc_codex_github_policy.py does (in place, no backup); rollback must put it
# back byte-for-byte (#1131).
printf '%s\n' 'sentinel = "KEEP-ME"' '' '[plugins."github@openai-curated-remote"]' 'enabled = true' > "$TMP/codex/config.toml"
CODEX_CFG_BEFORE="$(sha256sum "$TMP/codex/config.toml")"
rm -f "$CLAUDE/headless.sh"
INSTALLED_BEFORE="$(sha256sum "$CLAUDE/hooks/installed-hook.sh")"
cat > "$TMP/seed/setup.sh" <<'SH'
#!/usr/bin/env bash
printf '%s\n' 'partially-updated-hook' > "${CCC_CLAUDE_DIR:?}/hooks/installed-hook.sh"
printf '%s\n' 'partially-created-headless' > "${CCC_CLAUDE_DIR:?}/headless.sh"
printf '%s\n' '{"newHoncho":true}' > "${CCC_HERMES_DIR:?}/honcho.json"
mkdir -p "${CODEX_HOME:?}"
printf '%s\n' 'enabled = false # rewritten by policy' >> "${CODEX_HOME:?}/config.toml"
exit 1
SH
git -C "$TMP/seed" add -A && git -C "$TMP/seed" commit -qm broken-setup && git -C "$TMP/seed" push -q origin main
out="$(run_selfup run 2>&1)"; rc=$?
ok "setup failure exits non-zero and rolls back" '[ "$rc" = 6 ] && [ "$(git -C "$REPO" rev-parse HEAD)" = "$OLD_HEAD" ]'
ok "setup failure restores installed artifacts" '[ "$(sha256sum "$CLAUDE/hooks/installed-hook.sh")" = "$INSTALLED_BEFORE" ]'
ok "setup failure restores Hermes honcho artifact" 'grep -q "oldHoncho" "$HERMES/honcho.json"'
ok "setup failure restores Codex GitHub policy config byte-for-byte (#1131)" \
  '[ "$(sha256sum "$TMP/codex/config.toml")" = "$CODEX_CFG_BEFORE" ]'
ok "rollback notification names the restored scope honestly (#1131)" \
  'jq -r .text "$TMP/spool"/*SelfUpdate*.json | grep -q "Codex GitHub 정책 설정"'
ok "setup failure keeps managed absent artifact absent" '[ ! -e "$CLAUDE/headless.sh" ]'
# settings.local.json is node-local (unmanaged): self-update's snapshot/deploy/
# rollback lifecycle never touches it, so a node's approvals survive intact (#454).
ok "self-update leaves node-local settings.local.json untouched" \
  'grep -q "oldLocal" "$CLAUDE/settings.local.json"'
ok "rollback audit recorded" 'grep -q "setup-failed-rolled-back" "$STATE/self-update.log"'
ok "successful artifact rollback removes private recovery snapshot" \
  '! compgen -G "$STATE/self-update-install-rollback.*" >/dev/null'

# Same broken run on a node with NO $CODEX_DIR: the failed run creates the
# directory through the policy step, and the rollback must remove it whole
# rather than strand the new policy state (#1131).
rm -rf "$TMP/codex-fresh"
out="$(CODEX_HOME="$TMP/codex-fresh" run_selfup run 2>&1)"; rc=$?
ok "rollback removes a Codex dir the failed run created (#1131)" \
  '[ "$rc" = 6 ] && [ ! -e "$TMP/codex-fresh" ]'

# A failed repository reset must never be reported as a complete rollback.
cat > "$FAKEBIN/git" <<'SH'
#!/usr/bin/env bash
case " $* " in *" reset --hard "*) exit 96 ;; esac
exec /usr/bin/git "$@"
SH
chmod +x "$FAKEBIN/git"
out="$(PATH="$FAKEBIN:$PATH" run_selfup run 2>&1)"; rc=$?
ok "repo reset failure exits 9 and records degraded rollback" \
  '[ "$rc" = 9 ] && [ "$(git -C "$REPO" rev-parse HEAD)" != "$OLD_HEAD" ] && grep -q "repoRollback=false" "$STATE/self-update.log"'
ok "repo reset failure retains recovery snapshot directory" \
  'compgen -G "$STATE/self-update-install-rollback.*" >/dev/null'
rm -f "$FAKEBIN/git"
/usr/bin/git -C "$REPO" reset --hard -q "$OLD_HEAD"
rm -rf "$STATE"/self-update-install-rollback.*

# If extraction itself fails, expose a distinct degraded state and retain the
# validated private snapshot for operator recovery instead of deleting it in
# the EXIT cleanup trap.
cat > "$FAKEBIN/tar" <<'SH'
#!/usr/bin/env bash
[ "${1:-}" = "-xzf" ] && exit 97
exec /usr/bin/tar "$@"
SH
chmod +x "$FAKEBIN/tar"
out="$(PATH="$FAKEBIN:$PATH" run_selfup run 2>&1)"; rc=$?
ok "artifact restore failure exits 9 and records degraded rollback" \
  '[ "$rc" = 9 ] && grep -q "setup-failed-rollback-degraded" "$STATE/self-update.log"'
ok "degraded rollback retains validated private snapshot" \
  'compgen -G "$STATE/self-update-install-rollback.*" >/dev/null'
retained_snapshot="$(compgen -G "$STATE/self-update-install-rollback.*" | head -1)"
ok "retained recovery snapshot is owner-only" \
  '[ "$(stat -c %a "$retained_snapshot")" = 700 ] && [ "$(stat -c %a "$retained_snapshot/claude.tar.gz")" = 600 ] && [ "$(stat -c %a "$retained_snapshot/hermes.tar.gz")" = 600 ]'
rm -f "$FAKEBIN/tar"
rm -rf "$STATE"/self-update-install-rollback.*

# --- 5) fail-closed preconditions -----------------------------------------------
echo dirty > "$REPO/file.txt"
out="$(run_selfup run 2>&1)"; rc=$?
ok "dirty tree aborts" '[ "$rc" = 4 ] && grep -q "not clean" <<<"$out"'
git -C "$REPO" checkout -q -- file.txt
git -C "$REPO" checkout -q -b feature-x
out="$(run_selfup run 2>&1)"; rc=$?
ok "non-main branch aborts" '[ "$rc" = 4 ] && grep -q "expected .main." <<<"$out"'
git -C "$REPO" checkout -q main

# --- 6) status is read-only ------------------------------------------------------
out="$(run_selfup status)"; rc=$?
ok "status reports repo and services" '[ "$rc" = 0 ] && grep -q "repo: $REPO" <<<"$out" && grep -q "services file:" <<<"$out"'

# --- 7) idle gate: defer restarts while the bridge is serving a request --------
HFILE="$TMP/health.json"
export CCC_SELF_UPDATE_HEALTH_FILE="$HFILE"
# Bring the node fully up-to-date so a *proceed* is a clean exit-0 (no side effects).
git -C "$REPO" fetch -q origin main; git -C "$REPO" reset --hard -q origin/main
now_iso() { python3 -c "from datetime import datetime,timezone as z;print(datetime.now(z.utc).isoformat().replace('+00:00','Z'))"; }
old_iso() { python3 -c "from datetime import datetime,timezone as z,timedelta as d;print((datetime.now(z.utc)-d(seconds=600)).isoformat().replace('+00:00','Z'))"; }
mk_health() { printf '{"updated_at":"%s","workload":{"active_requests":%s,"oldest_request_age_seconds":%s}}' "$1" "$2" "$3" > "$HFILE"; }
clr_defer() { rm -f "$STATE/self-update.deferred-since"; }

clr_defer; mk_health "$(now_iso)" 2 45
out="$(run_selfup run 2>&1)"; rc=$?
ok "busy bridge defers (exit 8)" '[ "$rc" = 8 ] && grep -q "bridge busy" <<<"$out"'
ok "defer marker recorded" '[ -f "$STATE/self-update.deferred-since" ]'
ok "defer writes audit log" 'grep -q "deferred reason=bridge-busy" "$STATE/self-update.log"'

clr_defer; mk_health "$(now_iso)" 0 0
out="$(run_selfup run 2>&1)"; rc=$?
ok "idle bridge proceeds" '[ "$rc" = 0 ]'

clr_defer; mk_health "$(old_iso)" 3 45
out="$(run_selfup run 2>&1)"; rc=$?
ok "stale health proceeds (fail-open)" '[ "$rc" = 0 ]'

clr_defer; mk_health "$(now_iso)" 2 45
out="$(run_selfup run --force 2>&1)"; rc=$?
ok "--force bypasses idle gate" '[ "$rc" != 8 ]'

clr_defer; mk_health "$(now_iso)" 1 99999
out="$(run_selfup run 2>&1)"; rc=$?
ok "task older than busy-max proceeds" '[ "$rc" = 0 ]'

# total-deferral cap: continuous busy must not starve updates forever
mk_health "$(now_iso)" 1 60
echo "$(( $(date +%s) - 7200 ))" > "$STATE/self-update.deferred-since"
out="$(run_selfup run 2>&1)"; rc=$?
ok "deferral cap exceeded proceeds despite busy" '[ "$rc" = 0 ]'
ok "deferral marker cleared after proceeding" '[ ! -f "$STATE/self-update.deferred-since" ]'

# Back to the hermetic nonexistent health file (never the node's real one).
rm -f "$HFILE"; export CCC_SELF_UPDATE_HEALTH_FILE="$TMP/no-such-health.json"

# --- 8) #1060: terminal precondition aborts notify the owner ------------------
# A stalled node is invisible without these: the log is local, so before #1060
# the only detector was a human running a fleet-wide probe by hand.
spool_text() { cat "$TMP/spool"/*SelfUpdate*.json 2>/dev/null | jq -r .text 2>/dev/null; }
spool_dedup() { cat "$TMP/spool"/*SelfUpdate*.json 2>/dev/null | jq -r .dedup 2>/dev/null; }

rm -f "$TMP/spool"/*.json
git -C "$REPO" checkout -q -b sidetrack
out="$(run_selfup run 2>&1)"; rc=$?
ok "wrong-branch exits 4" '[ "$rc" = 4 ]'
ok "wrong-branch notifies" 'spool_text | grep -q "self-update 정지" && spool_text | grep -q "sidetrack"'
ok "wrong-branch dedup keys on reason, not SHA" 'spool_dedup | grep -qx "SelfUpdate:stalled-wrong-branch"'
# The abort line used to record only reason= and repo=, so a later reader could
# not tell a stray feature branch from a misconfigured branch setting (#1328).
ok "wrong-branch abort logs the offending branch and the expected one" \
  'grep -q "abort reason=wrong-branch .*branch=sidetrack expected=main" "$STATE/self-update.log"'
git -C "$REPO" checkout -q main
git -C "$REPO" branch -qD sidetrack

rm -f "$TMP/spool"/*.json
echo dirt > "$REPO/dirt.txt"
out="$(run_selfup run 2>&1)"; rc=$?
ok "dirty-tree exits 4" '[ "$rc" = 4 ]'
ok "dirty-tree notifies" 'spool_dedup | grep -qx "SelfUpdate:stalled-dirty-tree"'
rm -f "$REPO/dirt.txt"

rm -f "$TMP/spool"/*.json
out="$(REPO_OVERRIDE="$TMP/not-a-repo" run_selfup run 2>&1)"; rc=$?
ok "no-repo exits 4" '[ "$rc" = 4 ]'
ok "no-repo notifies" 'spool_dedup | grep -qx "SelfUpdate:stalled-no-repo"'

# fetch failure is transient: it must stay quiet until it has burned consecutive
# scheduled ticks, then alert.
rm -f "$TMP/spool"/*.json "$STATE/self-update.fetch-failures"
git -C "$REPO" remote set-url origin "$TMP/gone.git"
out="$(run_selfup run 2>&1)"; rc=$?
ok "fetch-failed exits 5" '[ "$rc" = 5 ]'
ok "first fetch failure stays quiet" '[ -z "$(spool_text)" ]'
ok "first fetch failure counted" '[ "$(cat "$STATE/self-update.fetch-failures")" = 1 ]'
out="$(run_selfup run 2>&1)"; rc=$?
ok "second consecutive fetch failure notifies" 'spool_dedup | grep -qx "SelfUpdate:stalled-fetch-failed"'
git -C "$REPO" remote set-url origin "$ORIGIN"
rm -f "$TMP/spool"/*.json
out="$(run_selfup run 2>&1)"; rc=$?
ok "successful fetch clears the failure counter" '[ ! -f "$STATE/self-update.fetch-failures" ]'

# The deferral path is NOT terminal — it self-heals next tick and must stay quiet.
rm -f "$TMP/spool"/*.json
export CCC_SELF_UPDATE_HEALTH_FILE="$HFILE"; clr_defer; mk_health "$(now_iso)" 2 45
out="$(run_selfup run 2>&1)"; rc=$?
ok "busy deferral still exits 8" '[ "$rc" = 8 ]'
ok "busy deferral does not notify" '[ -z "$(spool_text)" ]'
rm -f "$HFILE"; export CCC_SELF_UPDATE_HEALTH_FILE="$TMP/no-such-health.json"

# Restore the succeeding stub setup.sh (section 4 replaced it with `exit 1`).
cat > "$TMP/seed/setup.sh" <<'SH'
#!/usr/bin/env bash
echo "setup ran at $(git rev-parse --short HEAD)" >> "${SETUP_MARKER:?}"
SH

# --- #1328: conditional auto-recovery from a wrong-branch stall ----------------
# Safe shape (validated by the yukson 2026-08-27 recovery): stray branch fully
# pushed to origin + clean tree — switching back to main cannot lose anything.
# Every other wrong-branch shape must stay fail-closed.
# This section performs real update runs, so give them the working allowlist
# (section 3 left 'bad-unit', #971 left external restart/health commands) and
# the update path stays clean end-to-end.
printf '%s\n' 'hermes-broker' 'a2a-worker' > "$CLAUDE/self-update.services"
rm -f "$CLAUDE/self-update.restart-cmd" "$CLAUDE/self-update.health-cmd" \
      "$TMP/restart-should-not-run" "$TMP/runtime-healthy"

# (1) recoverable: fully pushed stray branch, clean tree -> back on main, update flows.
rm -f "$TMP/spool"/*.json
git -C "$REPO" checkout -q -b auto-recover-me
git -C "$REPO" push -q origin auto-recover-me
echo recover > "$TMP/seed/file.txt"
git -C "$TMP/seed" add -A && git -C "$TMP/seed" commit -qm six && git -C "$TMP/seed" push -q origin main
out="$(run_selfup run 2>&1)"; rc=$?
ok "fully-pushed stray branch auto-recovers (rc 0)" '[ "$rc" = 0 ]'
ok "recovery switched back to main" '[ "$(git -C "$REPO" symbolic-ref --short HEAD)" = "main" ]'
ok "recovery kept the update flowing (ff to origin/main)" \
  '[ "$(git -C "$REPO" rev-parse HEAD)" = "$(git -C "$TMP/seed" rev-parse HEAD)" ]'
ok "recovery logged with the stray branch name" \
  'grep -q "recover reason=wrong-branch from=auto-recover-me" "$STATE/self-update.log"'
ok "recovery notified the owner" 'grep -rh "자동 복구" "$TMP/spool" >/dev/null 2>&1'
ok "recovery leaves the stray branch ref in place (no destructive cleanup)" \
  'git -C "$REPO" rev-parse --verify --quiet refs/heads/auto-recover-me >/dev/null'

# (2) unpushed stray branch: the only copy of that commit is local -> fail-closed.
rm -f "$TMP/spool"/*.json
git -C "$REPO" checkout -q -b unpushed-work
echo local-only > "$REPO/wip.txt"
git -C "$REPO" add wip.txt && git -C "$REPO" commit -qm wip
out="$(run_selfup run 2>&1)"; rc=$?
ok "unpushed stray branch stays fail-closed (rc 4)" '[ "$rc" = 4 ]'
ok "unpushed stray branch aborts with both branch names logged" \
  'grep -q "abort reason=wrong-branch .*branch=unpushed-work expected=main" "$STATE/self-update.log"'
ok "unpushed stray branch still checked out, work intact" \
  '[ "$(git -C "$REPO" symbolic-ref --short HEAD)" = "unpushed-work" ] && git -C "$REPO" rev-parse --verify refs/heads/unpushed-work >/dev/null'
git -C "$REPO" checkout -q main
git -C "$REPO" branch -qD unpushed-work

# (3) dirty tree on a pushed stray branch: unknown local state -> fail-closed.
rm -f "$TMP/spool"/*.json
git -C "$REPO" checkout -q -b dirty-stray
git -C "$REPO" push -q origin dirty-stray
echo mess > "$REPO/file.txt"
out="$(run_selfup run 2>&1)"; rc=$?
ok "dirty tree on pushed stray branch stays fail-closed (rc 4)" '[ "$rc" = 4 ]'
ok "dirty stray branch still checked out" '[ "$(git -C "$REPO" symbolic-ref --short HEAD)" = "dirty-stray" ]'
git -C "$REPO" checkout -q -- file.txt
git -C "$REPO" checkout -q main
git -C "$REPO" branch -qD dirty-stray

# (4) main held by a linked worktree: git refuses the double checkout -> fail-closed.
git -C "$REPO" checkout -q -b stray-wt
git -C "$REPO" push -q origin stray-wt
git -C "$REPO" worktree add -q "$TMP/held-main" main
out="$(run_selfup run 2>&1)"; rc=$?
ok "main held by a linked worktree keeps fail-closed (rc 4)" '[ "$rc" = 4 ]'
ok "worktree-held main leaves the stray branch checked out" \
  '[ "$(git -C "$REPO" symbolic-ref --short HEAD)" = "stray-wt" ]'
git -C "$REPO" worktree remove --force "$TMP/held-main"
git -C "$REPO" checkout -q main
git -C "$REPO" branch -qD stray-wt

# (5) operator kill-switch restores the unconditional fail-closed abort.
rm -f "$TMP/spool"/*.json
git -C "$REPO" checkout -q -b switch-off
git -C "$REPO" push -q origin switch-off
out="$(CCC_SELF_UPDATE_AUTO_RECOVER=0 run_selfup run 2>&1)"; rc=$?
ok "CCC_SELF_UPDATE_AUTO_RECOVER=0 keeps fail-closed abort (rc 4)" '[ "$rc" = 4 ]'
ok "kill-switch abort notifies as stalled" 'spool_dedup | grep -qx "SelfUpdate:stalled-wrong-branch"'
git -C "$REPO" checkout -q main
git -C "$REPO" branch -qD switch-off

# --- #1081 phase 2: installer re-apply on gen drift --------------------------
# Plant the real gen-stamp lib + a stub installer in the fixture repo so
# self-update can recompute stamps and invoke a hermetic --apply.
mkdir -p "$TMP/seed/scripts/lib"
cp "$HERE/lib/installer-gen-stamp.sh" "$TMP/seed/scripts/lib/"
cat > "$TMP/seed/scripts/install-fake-cron.sh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=/dev/null
. "$ROOT/scripts/lib/installer-gen-stamp.sh"
GEN="$(ccc_installer_gen_stamp "$ROOT/scripts/install-fake-cron.sh")"
MARKER="# ccc-node:fake"
CRONTAB="${CCC_CRONTAB_CMD:-crontab}"
APPLY=0; REMOVE=0
for a in "$@"; do
  case "$a" in --apply) APPLY=1 ;; --remove) REMOVE=1 ;; esac
done
[ "$APPLY" = 1 ] || exit 0
: >> "${CCC_TEST_REAPPLY_MARKER:?}"
[ -z "${CCC_TEST_REAPPLY_FAIL:-}" ] || exit 1
current="$("$CRONTAB" -l 2>/dev/null || true)"
without="$(printf '%s\n' "$current" | grep -vF "$MARKER" || true)"
if [ "$REMOVE" = 1 ]; then
  printf '%s\n' "$without" | "$CRONTAB" -
else
  printf '%s\n%s\n' "$without" "* * * * * echo fake $MARKER gen=$GEN" | "$CRONTAB" -
fi
SH
chmod +x "$TMP/seed/scripts/install-fake-cron.sh"
git -C "$TMP/seed" add -A && git -C "$TMP/seed" commit -qm reapply-plant && git -C "$TMP/seed" push -q origin main
# shellcheck source=/dev/null
. "$HERE/lib/installer-gen-stamp.sh"
fake_gen="$(ccc_installer_gen_stamp "$TMP/seed/scripts/install-fake-cron.sh")"

# matching gen: no replay
printf '%s\n' 'hermes-broker' > "$CLAUDE/self-update.services"
export CCC_TEST_REAPPLY_MARKER="$TMP/reapplied.marker"
rm -f "$CCC_TEST_REAPPLY_MARKER" "$TMP/spool"/*.json
printf '%s\n' '0 4 * * * echo keepme' > "$FAKE_CRON"
jq -nc --arg gen "$fake_gen" \
  '{schema:"ccc.install-record.v1",installer:"scripts/install-fake-cron.sh",marker:"# ccc-node:fake",gen:$gen,argv:["--apply"],applied_at:"2026-01-01T00:00:00Z"}' \
  > "$STATE/install-fake-cron.json"
out="$(run_selfup run)"; rc=$?
ok "matching gen does not re-apply" '[ "$rc" = 0 ] && [ ! -f "$CCC_TEST_REAPPLY_MARKER" ]'
ok "matching-gen tick leaves unrelated crontab lines alone" 'grep -qF "echo keepme" "$FAKE_CRON"'

# drifted gen: replay + stamp + notify
echo drift-reapply > "$TMP/seed/file.txt"
git -C "$TMP/seed" add -A && git -C "$TMP/seed" commit -qm reapply-drift && git -C "$TMP/seed" push -q origin main
rm -f "$CCC_TEST_REAPPLY_MARKER" "$TMP/spool"/*.json
jq -nc '{schema:"ccc.install-record.v1",installer:"scripts/install-fake-cron.sh",marker:"# ccc-node:fake",gen:"h_000000000000",argv:["--apply"],applied_at:"2026-01-01T00:00:00Z"}' \
  > "$STATE/install-fake-cron.json"
out="$(run_selfup run)"; rc=$?
ok "drifted gen re-applies and exits 0" '[ "$rc" = 0 ] && [ -f "$CCC_TEST_REAPPLY_MARKER" ]'
ok "re-apply stamps the current gen onto the managed line" 'grep -qF "# ccc-node:fake gen=$fake_gen" "$FAKE_CRON"'
ok "re-apply preserves unrelated crontab lines" 'grep -qF "echo keepme" "$FAKE_CRON"'
ok "re-apply is mentioned in the success notify" 'grep -rh "cron 재적용" "$TMP/spool" >/dev/null 2>&1'

# kill-switch file
echo noswitch > "$TMP/seed/file.txt"
git -C "$TMP/seed" add -A && git -C "$TMP/seed" commit -qm reapply-killfile && git -C "$TMP/seed" push -q origin main
: > "$CLAUDE/self-update.no-reapply"
rm -f "$CCC_TEST_REAPPLY_MARKER" "$TMP/spool"/*.json
jq -nc '{schema:"ccc.install-record.v1",installer:"scripts/install-fake-cron.sh",marker:"# ccc-node:fake",gen:"h_000000000000",argv:["--apply"],applied_at:"2026-01-01T00:00:00Z"}' \
  > "$STATE/install-fake-cron.json"
out="$(run_selfup run)"; rc=$?
ok "operator no-reapply file skips replay" '[ "$rc" = 0 ] && [ ! -f "$CCC_TEST_REAPPLY_MARKER" ]'
rm -f "$CLAUDE/self-update.no-reapply"

# env kill-switch
echo noenv > "$TMP/seed/file.txt"
git -C "$TMP/seed" add -A && git -C "$TMP/seed" commit -qm reapply-killenv && git -C "$TMP/seed" push -q origin main
rm -f "$CCC_TEST_REAPPLY_MARKER"
out="$(CCC_SELF_UPDATE_REAPPLY=0 run_selfup run)"; rc=$?
ok "CCC_SELF_UPDATE_REAPPLY=0 skips replay" '[ "$rc" = 0 ] && [ ! -f "$CCC_TEST_REAPPLY_MARKER" ]'

# replay failure restores crontab
echo failre > "$TMP/seed/file.txt"
git -C "$TMP/seed" add -A && git -C "$TMP/seed" commit -qm reapply-fail && git -C "$TMP/seed" push -q origin main
printf '%s\n' '0 4 * * * echo keepme' > "$FAKE_CRON"
rm -f "$CCC_TEST_REAPPLY_MARKER" "$TMP/spool"/*.json
jq -nc '{schema:"ccc.install-record.v1",installer:"scripts/install-fake-cron.sh",marker:"# ccc-node:fake",gen:"h_000000000000",argv:["--apply"],applied_at:"2026-01-01T00:00:00Z"}' \
  > "$STATE/install-fake-cron.json"
out="$(CCC_TEST_REAPPLY_FAIL=1 run_selfup run 2>&1)"; rc=$?
ok "failed re-apply exits 12" '[ "$rc" = 12 ]'
ok "failed re-apply restores the pre-reapply crontab" 'grep -qF "echo keepme" "$FAKE_CRON" && ! grep -qF "ccc-node:fake" "$FAKE_CRON"'
ok "failed re-apply notifies" 'grep -rh "cron 재적용 실패" "$TMP/spool" >/dev/null 2>&1'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
