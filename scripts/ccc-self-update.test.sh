#!/usr/bin/env bash
# Tests for ccc-self-update.sh — hermetic: fixture git repos + fake systemctl.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SELFUP="$HERE/ccc-self-update.sh"
pass=0; fail=0
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t

# Keep fixtures hermetic when the operator shell exports live self-update
# settings. In particular, a real busy health file must not defer every fixture
# update before the tests install their own health file in section 7.
unset CCC_SELF_UPDATE_BRANCH CCC_SELF_UPDATE_SERVICES
unset CCC_SELF_UPDATE_HEALTH_FILE CCC_SELF_UPDATE_HEALTH_FRESH_SECONDS
unset CCC_SELF_UPDATE_BUSY_MAX_SECONDS CCC_SELF_UPDATE_MAX_DEFER_SECONDS

# Fixture: origin repo with a stub setup.sh, plus a node-side clone.
ORIGIN="$TMP/origin.git"
REPO="$TMP/node/ccc-node"
git init -q --bare "$ORIGIN"
git init -q -b main "$TMP/seed"
cat > "$TMP/seed/setup.sh" <<'SH'
#!/usr/bin/env bash
echo "setup ran at $(git rev-parse --short HEAD)" >> "${SETUP_MARKER:?}"
SH
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
case "\$*" in *bad*) exit 1 ;; esac
exit 0
SH
chmod +x "$FAKEBIN/fakesystemctl"

CLAUDE="$TMP/claude"
STATE="$CLAUDE/state"
HERMES="$TMP/hermes"
mkdir -p "$STATE" "$CLAUDE" "$HERMES"
export SETUP_MARKER="$TMP/setup.marker"

run_selfup() {
  CCC_CLAUDE_DIR="$CLAUDE" CCC_STATE_DIR="$STATE" CCC_PUSH_SPOOL="$TMP/spool" \
  CCC_HERMES_DIR="$HERMES" \
  CCC_SELF_UPDATE_REPO="$REPO" CCC_SELF_UPDATE_SYSTEMCTL="$FAKEBIN/fakesystemctl" \
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
ok "audit record written" 'grep -q "\"result\":\"ok\"" "$STATE/self-update.log"'
ok "owner notification queued" 'ls "$TMP/spool"/*SelfUpdate*.json >/dev/null 2>&1 && jq -r .text "$TMP/spool"/*SelfUpdate*.json | grep -q "self-update 완료"'
ok "successful update removes private recovery snapshot" \
  '! compgen -G "$STATE/self-update-install-rollback.*" >/dev/null'

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

# --- 4) setup.sh failure rolls back --------------------------------------------
OLD_HEAD="$(git -C "$REPO" rev-parse HEAD)"
mkdir -p "$CLAUDE/hooks"
printf '%s\n' 'old-installed-hook' > "$CLAUDE/hooks/installed-hook.sh"
printf '%s\n' '{"oldHoncho":true}' > "$HERMES/honcho.json"
printf '%s\n' '{"oldLocal":true}' > "$CLAUDE/settings.local.json"
rm -f "$CLAUDE/headless.sh"
INSTALLED_BEFORE="$(sha256sum "$CLAUDE/hooks/installed-hook.sh")"
cat > "$TMP/seed/setup.sh" <<'SH'
#!/usr/bin/env bash
printf '%s\n' 'partially-updated-hook' > "${CCC_CLAUDE_DIR:?}/hooks/installed-hook.sh"
printf '%s\n' 'partially-created-headless' > "${CCC_CLAUDE_DIR:?}/headless.sh"
printf '%s\n' '{"newHoncho":true}' > "${CCC_HERMES_DIR:?}/honcho.json"
exit 1
SH
git -C "$TMP/seed" add -A && git -C "$TMP/seed" commit -qm broken-setup && git -C "$TMP/seed" push -q origin main
out="$(run_selfup run 2>&1)"; rc=$?
ok "setup failure exits non-zero and rolls back" '[ "$rc" = 6 ] && [ "$(git -C "$REPO" rev-parse HEAD)" = "$OLD_HEAD" ]'
ok "setup failure restores installed artifacts" '[ "$(sha256sum "$CLAUDE/hooks/installed-hook.sh")" = "$INSTALLED_BEFORE" ]'
ok "setup failure restores Hermes honcho artifact" 'grep -q "oldHoncho" "$HERMES/honcho.json"'
ok "setup failure keeps managed absent artifact absent" '[ ! -e "$CLAUDE/headless.sh" ]'
# settings.local.json is node-local (unmanaged): self-update's snapshot/deploy/
# rollback lifecycle never touches it, so a node's approvals survive intact (#454).
ok "self-update leaves node-local settings.local.json untouched" \
  'grep -q "oldLocal" "$CLAUDE/settings.local.json"'
ok "rollback audit recorded" 'grep -q "setup-failed-rolled-back" "$STATE/self-update.log"'
ok "successful artifact rollback removes private recovery snapshot" \
  '! compgen -G "$STATE/self-update-install-rollback.*" >/dev/null'

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

rm -f "$HFILE"; unset CCC_SELF_UPDATE_HEALTH_FILE

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
