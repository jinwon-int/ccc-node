#!/usr/bin/env bash
# shellcheck disable=SC2034  # out*/rc/UNIT/HD/CFG are read via eval inside ok()
# Tests for install-hande-bridge.sh — hermetic: a fake root under $TMP holds the
# Matrix unit + drop-ins to clone, stub systemctl / systemd-tmpfiles record calls,
# ~/.claude lists live in the fake HOME. Nothing real is installed or started.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/install-hande-bridge.sh"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$HERE/../claude/hooks/lib/test-stub.sh"
ccc_test_reset_hook_env
pass=0; fail=0
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT

ROOT="$TMP/root"; CLAUDE="$TMP/home/.claude"; CCCNODE="$TMP/opt/ccc-node"; CALLS="$TMP/calls"
mkdir -p "$ROOT/etc/systemd/system/ccc-matrix-bridge.service.d" "$ROOT/run" "$CLAUDE" "$CCCNODE/bridge/core/hande"
printf 'matrix\n' > "$CLAUDE/frontends"
printf 'a2a-hermes-worker.service\nccc-matrix-bridge.service\n' > "$CLAUDE/self-update.services"
cat > "$ROOT/etc/systemd/system/ccc-matrix-bridge.service" <<'EOF'
[Unit]
Description=ccc-node Matrix frontend (family messenger, CCC_CHANNEL=matrix)
After=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/ccc-node/bridge
Environment=HOME=/root
Environment=PROJECT_ROOT=/root
Environment=BOT_DATA_DIR=/root/.ccc-matrix
Environment=CCC_CHANNEL=matrix
Environment=CCC_MATRIX_CONFIG_PATH=/etc/family-matrix/config.json
Environment=CCC_MATRIX_INITIALIZE=0
ExecStart=/opt/ccc-node/bridge/venv/bin/python -m telegram_bot --path /root
Restart=always

[Install]
WantedBy=multi-user.target
EOF
D="$ROOT/etc/systemd/system/ccc-matrix-bridge.service.d"
printf '[Service]\nEnvironment=CCC_PUSH_ENABLED=true\n' > "$D/push.conf"
printf '[Service]\nEnvironment=CCC_PUSH_FLEET_NODE=x\n' > "$D/70-fleet-alert-relay.conf"
printf '[Service]\nEnvironmentFile=-/root/.config/ccc-node/matrix-nudge.env\n' > "$D/60-webhook-nudge.conf"
printf '[Service]\nEnvironment="CCC_BRIDGE_EXECUTION_PROFILE=owner-operator"\nEnvironment="ALLOWED_USER_IDS=7"\n' > "$D/zz-parity.conf"
printf '[Service]\nEnvironmentFile=/root/.config/ccc-main/matrix-opus55-high.env\nEnvironment=CCC_MATRIX_STARTUP_BANNER=false\nEnvironment=CCC_X_PATH=/root/.ccc-matrix/x\n' > "$D/zzz-provider.conf"

mkdir -p "$TMP/bin"
cat > "$TMP/bin/systemctl" <<EOF
#!/usr/bin/env bash
echo "systemctl \$*" >> "$CALLS"
case "\$1" in show) echo active ;; esac
exit 0
EOF
cat > "$TMP/bin/systemd-tmpfiles" <<EOF
#!/usr/bin/env bash
echo "tmpfiles \$*" >> "$CALLS"
# honour the one line we wrote: d <path> <mode> <user> <group> -
line="\$(grep -v '^#' "\$2" | grep '^d ')"
set -- \$line
mkdir -p "$ROOT\$2"; chmod "\$3" "$ROOT\$2"
EOF
chmod +x "$TMP/bin/"*
group="$(id -gn)"
run_install() {
  CCC_HANDE_INSTALL_ROOT="$ROOT" CCC_HANDE_SYSTEMCTL="$TMP/bin/systemctl" CCC_HANDE_TMPFILES="$TMP/bin/systemd-tmpfiles" \
  CCC_HANDE_CCC_NODE="$CCCNODE" CCC_HANDE_CLAUDE_DIR="$CLAUDE" CCC_HANDE_SETTLE_SECONDS=0 \
  bash "$SCRIPT" --bot-group "$group" "$@"
}
run_install_auto() { # no CCC_HANDE_CCC_NODE: the checkout must come from the cloned unit (#2526)
  CCC_HANDE_INSTALL_ROOT="$ROOT" CCC_HANDE_SYSTEMCTL="$TMP/bin/systemctl" CCC_HANDE_TMPFILES="$TMP/bin/systemd-tmpfiles" \
  CCC_HANDE_CLAUDE_DIR="$CLAUDE" CCC_HANDE_SETTLE_SECONDS=0 \
  bash "$SCRIPT" --bot-group "$group" "$@"
}

# ---- preflight refusals ----------------------------------------------------------
out="$(run_install --node 'Bad Name' 2>&1)"; rc=$?
ok "bad node name is refused" '[ "$rc" != 0 ] && grep -q "node must match" <<<"$out"'
mv "$CCCNODE/bridge/core/hande" "$CCCNODE/bridge/core/hande.off"
out="$(run_install --node nodea 2>&1)"; rc=$?
ok "missing hande code is refused" '[ "$rc" != 0 ] && grep -q "no bridge/core/hande" <<<"$out"'
mv "$CCCNODE/bridge/core/hande.off" "$CCCNODE/bridge/core/hande"

# ---- #2526: checkout comes from the cloned unit, not a fixed /opt/ccc-node -------------
# A /root/ccc-node-style node: the Matrix unit's WorkingDirectory names the checkout.
ALT="$TMP/alt/ccc-node"; mkdir -p "$ALT/bridge/core/hande"
sed -e "s|/opt/ccc-node|$ALT|g" "$ROOT/etc/systemd/system/ccc-matrix-bridge.service" > "$ROOT/etc/systemd/system/ccc-matrix-alt.service"
out="$(run_install_auto --node nodea --clone-from ccc-matrix-alt.service --dry-run 2>&1)"; rc=$?
ok "#2526: checkout detected from the unit's WorkingDirectory (no seam needed)" '[ "$rc" = 0 ] && grep -q "checkout=$ALT (unit-WorkingDirectory)" <<<"$out"'
# WorkingDirectory absent → the ExecStart interpreter path names it.
sed -i '/^WorkingDirectory=/d' "$ROOT/etc/systemd/system/ccc-matrix-alt.service"
out="$(run_install_auto --node nodea --clone-from ccc-matrix-alt.service --dry-run 2>&1)"; rc=$?
ok "#2526: checkout detected from ExecStart when WorkingDirectory is absent" '[ "$rc" = 0 ] && grep -q "checkout=$ALT (unit-ExecStart)" <<<"$out"'
# The hande code is checked in THAT checkout; the refusal names the source of the path.
rm -rf "$ALT/bridge/core/hande"
out="$(run_install_auto --node nodea --clone-from ccc-matrix-alt.service --dry-run 2>&1)"; rc=$?
ok "#2526: missing hande code in the detected checkout is refused with the path and its source" '[ "$rc" != 0 ] && grep -q "$ALT (from unit-ExecStart) has no bridge/core/hande" <<<"$out"'
# Neither hint → the old /opt/ccc-node default (named as such); the seam still overrides.
sed -i 's|^ExecStart=.*$|ExecStart=/usr/bin/env python3 -m telegram_bot --path /root|' "$ROOT/etc/systemd/system/ccc-matrix-alt.service"
out="$(run_install_auto --node nodea --clone-from ccc-matrix-alt.service --dry-run 2>&1)"; rc=$?
# (the host may or may not have a real /opt/ccc-node checkout — either branch must name the default as the source)
ok "#2526: no hint in the unit → /opt/ccc-node default, named as such" 'grep -qE "checkout=/opt/ccc-node \(default\)|/opt/ccc-node \(from default\) has no bridge/core/hande" <<<"$out"'
out="$(run_install --node nodea --clone-from ccc-matrix-alt.service --dry-run 2>&1)"; rc=$?
ok "#2526: CCC_HANDE_CCC_NODE still overrides detection" '[ "$rc" = 0 ] && grep -q "checkout=$CCCNODE (CCC_HANDE_CCC_NODE)" <<<"$out"'
rm -f "$ROOT/etc/systemd/system/ccc-matrix-alt.service"
out="$(run_install --node nodea --bot-group no-such-group-zz 2>&1)"; rc=$?
ok "missing bot group is refused" '[ "$rc" != 0 ] && grep -q "does not exist" <<<"$out"'
out="$(run_install --node nodea --family-user owner 2>&1)"; rc=$?
ok "malformed family user is refused" '[ "$rc" != 0 ] && grep -q "family-user must" <<<"$out"'

# ---- dry run writes nothing -----------------------------------------------------------
out="$(run_install --node nodea --dry-run 2>&1)"; rc=$?
ok "dry-run exits 0" '[ "$rc" = 0 ]'
ok "dry-run writes no unit" '[ ! -f "$ROOT/etc/systemd/system/ccc-hande-bridge.service" ]'
ok "dry-run does not call systemctl" '[ ! -f "$CALLS" ]'

# ---- real run -----------------------------------------------------------------------
out="$(run_install --node nodea --family-user @kid:hande --family-user @mom:hande 2>&1)"; rc=$?
ok "install exits 0" '[ "$rc" = 0 ] || { echo "$out"; false; }'
UNIT="$ROOT/etc/systemd/system/ccc-hande-bridge.service"
ok "unit written" '[ -f "$UNIT" ]'
ok "unit: channel hande" 'grep -qx "Environment=CCC_CHANNEL=hande" "$UNIT"'
ok "unit: data dir renamed" 'grep -qx "Environment=BOT_DATA_DIR=/root/.ccc-hande" "$UNIT"'
ok "unit: hande config path" 'grep -qx "Environment=CCC_HANDE_CONFIG_PATH=/etc/ccc-hande/config.json" "$UNIT"'
ok "unit: socket path" 'grep -qx "Environment=CCC_HANDE_SOCKET_PATH=/run/ccc-hande/responder.sock" "$UNIT"'
ok "unit: no CCC_MATRIX_* left" '! grep -q "CCC_MATRIX_" "$UNIT"'
ok "unit: ExecStart/User/HOME kept" 'grep -qx "User=root" "$UNIT" && grep -qx "Environment=HOME=/root" "$UNIT" && grep -q "^ExecStart=/opt/ccc-node/bridge/venv/bin/python -m telegram_bot --path /root$" "$UNIT"'
ok "unit: description replaced" 'grep -q "^Description=ccc-node hande frontend" "$UNIT"'
HD="$ROOT/etc/systemd/system/ccc-hande-bridge.service.d"
ok "drop-ins: provider + parity copied" '[ -f "$HD/zz-parity.conf" ] && [ -f "$HD/zzz-provider.conf" ]'
ok "drop-ins: push / relay / nudge skipped" '[ ! -f "$HD/push.conf" ] && [ ! -f "$HD/70-fleet-alert-relay.conf" ] && [ ! -f "$HD/60-webhook-nudge.conf" ]'
ok "drop-ins: CCC_MATRIX_* lines dropped, .ccc-matrix paths renamed" '! grep -q "CCC_MATRIX_STARTUP_BANNER" "$HD/zzz-provider.conf" && grep -q "/root/.ccc-hande/x" "$HD/zzz-provider.conf"'
ok "drop-ins: pin disables push" 'grep -qx "Environment=CCC_PUSH_ENABLED=false" "$HD"/zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz-hande.conf'
CFG="$ROOT/etc/ccc-hande/config.json"
ok "config written 0600" '[ "$(stat -c %a "$CFG")" = 600 ]'
ok "config content" 'python3 - "$CFG" "$group" <<'"'"'PY'"'"'
import json, sys
d = json.load(open(sys.argv[1]))
assert d["account"] == "@nodea:hande" and d["owner"] == "@owner:hande"
assert d["rooms"] == ["nodea"] and d["family_users"] == ["@kid:hande", "@mom:hande"]
assert d["socket_path"] == "/run/ccc-hande/responder.sock" and d["socket_group"] == sys.argv[2]
assert d["approval_timeout_s"] == 600 and d["tool_timeout_s"] == 90 and d["steward_tools"] is True
PY'
ok "tmpfiles conf written and dir created with setgid" 'grep -q "^d /run/ccc-hande 2770 root $group -$" "$ROOT/etc/tmpfiles.d/ccc-hande.conf" && [ -d "$ROOT/run/ccc-hande" ]'
ok "frontends gained hande" 'grep -qx hande "$CLAUDE/frontends" && grep -qx matrix "$CLAUDE/frontends"'
ok "self-update.services gained the unit" 'grep -qx ccc-hande-bridge.service "$CLAUDE/self-update.services"'
ok "backups of the lists exist" 'ls "$CLAUDE"/frontends.bak-hande-* "$CLAUDE"/self-update.services.bak-hande-* >/dev/null 2>&1'
ok "systemctl: daemon-reload then enable --now" 'grep -q "^systemctl daemon-reload$" "$CALLS" && grep -q "^systemctl enable --now ccc-hande-bridge.service$" "$CALLS"'
ok "report names step 5 and rollback" 'grep -q "step 5" <<<"$out" && grep -q "rollback:" <<<"$out"'

# ---- idempotent second run ----------------------------------------------------------
out2="$(run_install --node nodea --family-user @kid:hande --family-user @mom:hande 2>&1)"; rc=$?
ok "second run exits 0" '[ "$rc" = 0 ]'
ok "second run does not duplicate list lines" '[ "$(grep -cx hande "$CLAUDE/frontends")" = 1 ] && [ "$(grep -cx ccc-hande-bridge.service "$CLAUDE/self-update.services")" = 1 ]'

# ---- --room / --no-start --------------------------------------------------------------
: > "$CALLS"
out3="$(run_install --node nodeb --room agent --no-start 2>&1)"; rc=$?
ok "--no-start reloads but does not enable" '[ "$rc" = 0 ] && grep -q daemon-reload "$CALLS" && ! grep -q "enable --now" "$CALLS"'
ok "--room overrides the room name" 'python3 -c "import json,sys; d=json.load(open(sys.argv[1])); assert d[\"rooms\"]==[\"agent\"] and d[\"account\"]==\"@nodeb:hande\"" "$CFG"'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
