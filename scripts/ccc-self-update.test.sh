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

STATE="$TMP/state"
CLAUDE="$TMP/claude"
mkdir -p "$STATE" "$CLAUDE"
export SETUP_MARKER="$TMP/setup.marker"

run_selfup() {
  CCC_CLAUDE_DIR="$CLAUDE" CCC_STATE_DIR="$STATE" CCC_PUSH_SPOOL="$TMP/spool" \
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

# --- 3) service restart failure is reported ------------------------------------
echo change3 > "$TMP/seed/file.txt"
git -C "$TMP/seed" add -A && git -C "$TMP/seed" commit -qm three && git -C "$TMP/seed" push -q origin main
printf '%s\n' 'bad-unit' > "$CLAUDE/self-update.services"
rm -f "$TMP/spool"/*.json
out="$(run_selfup run 2>&1)"; rc=$?
ok "restart failure exits non-zero" '[ "$rc" = 7 ] && grep -q "failed to restart" <<<"$out"'
ok "failure notification queued" 'jq -r .text "$TMP/spool"/*SelfUpdate*.json 2>/dev/null | grep -q "재시작 실패"'

# --- 4) setup.sh failure rolls back --------------------------------------------
OLD_HEAD="$(git -C "$REPO" rev-parse HEAD)"
cat > "$TMP/seed/setup.sh" <<'SH'
#!/usr/bin/env bash
exit 1
SH
git -C "$TMP/seed" add -A && git -C "$TMP/seed" commit -qm broken-setup && git -C "$TMP/seed" push -q origin main
out="$(run_selfup run 2>&1)"; rc=$?
ok "setup failure exits non-zero and rolls back" '[ "$rc" = 6 ] && [ "$(git -C "$REPO" rev-parse HEAD)" = "$OLD_HEAD" ]'
ok "rollback audit recorded" 'grep -q "setup-failed-rolled-back" "$STATE/self-update.log"'

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

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
