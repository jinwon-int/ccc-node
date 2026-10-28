#!/usr/bin/env bash
# shellcheck disable=SC2034  # out*/rc/RUN/CFG/SVC are read via eval inside ok()
# Tests for install-hande-bridge-termux.sh — hermetic: a fake Termux HOME/PREFIX
# under $TMP holds a run-matrix launcher shaped like the fleet's (gongyung,
# 2026-10-28; placeholder values), a fake `sv` records calls, bridge-current points
# at a fake generation. No Termux, no runit, nothing real started.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/install-hande-bridge-termux.sh"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$HERE/../claude/hooks/lib/test-stub.sh"
ccc_test_reset_hook_env
pass=0; fail=0
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT

export HOME="$TMP/home" PREFIX="$TMP/prefix"
export SVDIR="$PREFIX/var/service"
H="$HOME"; P="$PREFIX"; CALLS="$TMP/calls"; CLAUDE="$H/.claude"
GEN="$H/.ccc-node/preparations/main-abc1234-20261028"
mkdir -p "$H/.local/share/ccc-matrix" "$H/.ccc-node/scripts" "$GEN/source/bridge/core/hande" "$GEN/job/runtime/bin" \
         "$SVDIR/ccc-matrix-bridge" "$P/bin" "$CLAUDE" "$H/.config/ccc-node"
ln -sfn "preparations/main-abc1234-20261028" "$H/.ccc-node/bridge-current"
printf 'matrix\n' > "$CLAUDE/frontends"

# run-matrix as the fleet writes it (one exec line, env -i, relay/nudge sourcing)
cat > "$H/.local/share/ccc-matrix/run-matrix" <<EOF
#!$P/bin/sh
set -eu
umask 077
case "\${1:-}" in "") initialize=0;; --initialize) initialize=1;; *) exit 64;; esac
# Serving generation resolved via ~/.ccc-node/bridge-current (2026-10-08)
CCC_SRC="\$($H/.ccc-node/scripts/bridge-current.sh --print-source)"
CCC_JOB="\$($H/.ccc-node/scripts/bridge-current.sh --print-job)"
cd "\$CCC_SRC/bridge"
# ccc-node#2083 Matrix webhook nudge
NUDGE_ENV=$H/.config/ccc-node/matrix-nudge.env
if [ -r "\$NUDGE_ENV" ]; then . "\$NUDGE_ENV"; fi
# ccc-node#2182 fleet alert relay (spool records -> @fleet-alerts, not the agent room)
RELAY_ENV=$H/.config/ccc-node/fleet-alert-relay.env
CCC_PUSH_FLEET_RELAY_URL=""; CCC_PUSH_FLEET_RELAY_SECRET_FILE=""; CCC_PUSH_FLEET_NODE=""
if [ -r "\$RELAY_ENV" ]; then set -a; . "\$RELAY_ENV"; set +a; fi
exec $P/bin/env -i CCC_PUSH_FLEET_RELAY_URL="\$CCC_PUSH_FLEET_RELAY_URL" CCC_PUSH_FLEET_RELAY_SECRET_FILE="\$CCC_PUSH_FLEET_RELAY_SECRET_FILE" CCC_PUSH_FLEET_NODE="\$CCC_PUSH_FLEET_NODE" CCC_PUSH_FLEET_RELAY_DIRECT_EVENTS="\${CCC_PUSH_FLEET_RELAY_DIRECT_EVENTS:-}" CCC_WEBHOOK_NUDGE_ENABLED="\${CCC_WEBHOOK_NUDGE_ENABLED:-false}" CCC_WEBHOOK_NUDGE_HOST="\${CCC_WEBHOOK_NUDGE_HOST:-127.0.0.1}" CCC_WEBHOOK_NUDGE_SECRET="\${CCC_WEBHOOK_NUDGE_SECRET:-}" CCC_MATRIX_WEBHOOK_NUDGE_PORT="\${CCC_MATRIX_WEBHOOK_NUDGE_PORT:-}" ANDROID_DATA=/data BOT_DATA_DIR=$H/.ccc-matrix CCC_AGENT_PROVIDER=claude CCC_BOT_ENV_FILE=$H/ccc-node/bridge/.env CCC_BRIDGE_EXECUTION_PROFILE=owner-operator CCC_CHANNEL=matrix CCC_CLAUDE_DIR=$H/.claude CCC_MATRIX_CONFIG_PATH=$H/.config/family-matrix/config.json CCC_MATRIX_STARTUP_BANNER=false CCC_PUSH_ENABLED=true CCC_PUSH_SPOOL=$H/.telegram_bot/push-spool CCC_REQUIRE_ALLOWLIST=true HOME=$H LANG=C.UTF-8 LOGS_DIR=$H/.ccc-matrix/logs PATH=$GEN/job/runtime/bin:$P/bin PREFIX=$P PYTHONUNBUFFERED=1 SESSION_STORE_PATH=$H/.ccc-matrix/sessions.json TMPDIR=$P/tmp ANTHROPIC_MODEL=claude-x CLAUDE_CLI_PATH=$H/.local/share/ccc-main-claude/1.0/claude CCC_MATRIX_INITIALIZE="\$initialize" "\$CCC_JOB/runtime/bin/python" -m telegram_bot --path $H
EOF

# fake sv: records calls; `status` answers a runit line
cat > "$P/bin/sv" <<EOF
#!/usr/bin/env bash
echo "sv \$*" >> "$CALLS"
case "\$1" in status) echo "run: \$2: (pid 4242) 7s; run: log: (pid 4243) 7s" ;; esac
exit 0
EOF
chmod +x "$P/bin/sv"
run_install() { CCC_HANDE_TERMUX_SV="$P/bin/sv" CCC_HANDE_SETTLE_SECONDS=0 bash "$SCRIPT" "$@"; }
RUN="$H/.local/share/ccc-hande/run-hande"; CFG="$H/.config/ccc-hande/config.json"; SVC="$SVDIR/ccc-hande-bridge"

# ---- preflight refusals ----------------------------------------------------------
out="$(run_install --node 'Bad Name' 2>&1)"; rc=$?
ok "bad node name is refused" '[ "$rc" != 0 ] && grep -q "node must match" <<<"$out"'
mv "$H/.local/share/ccc-matrix/run-matrix" "$TMP/run-matrix.keep"
out="$(run_install --node nodea 2>&1)"; rc=$?
ok "missing run-matrix is refused" '[ "$rc" != 0 ] && grep -q "no $H/.local/share/ccc-matrix/run-matrix to clone" <<<"$out"'
mv "$TMP/run-matrix.keep" "$H/.local/share/ccc-matrix/run-matrix"
mv "$GEN/source/bridge/core/hande" "$GEN/source/bridge/core/hande.off"
out="$(run_install --node nodea 2>&1)"; rc=$?
ok "serving generation without hande code is refused" '[ "$rc" != 0 ] && grep -q "has no bridge/core/hande" <<<"$out"'
mv "$GEN/source/bridge/core/hande.off" "$GEN/source/bridge/core/hande"
rm "$H/.ccc-node/bridge-current"
out="$(run_install --node nodea 2>&1)"; rc=$?
ok "unresolved bridge-current is refused" '[ "$rc" != 0 ] && grep -q "does not resolve" <<<"$out"'
ln -sfn "preparations/main-abc1234-20261028" "$H/.ccc-node/bridge-current"
out="$(run_install --node nodea --family-user owner 2>&1)"; rc=$?
ok "malformed family user is refused" '[ "$rc" != 0 ] && grep -q "family-user must" <<<"$out"'

# ---- dry run writes nothing -----------------------------------------------------------
out="$(run_install --node nodea --dry-run 2>&1)"; rc=$?
ok "dry-run exits 0 and names the generation" '[ "$rc" = 0 ] && grep -q "generation=main-abc1234-20261028" <<<"$out"'
ok "dry-run writes nothing" '[ ! -e "$RUN" ] && [ ! -e "$CFG" ] && [ ! -e "$SVC" ] && [ ! -f "$CALLS" ]'

# ---- real run -----------------------------------------------------------------------
out="$(run_install --node nodea --family-user @kid:hande 2>&1)"; rc=$?
ok "install exits 0" '[ "$rc" = 0 ] || { echo "$out"; false; }'
ok "run-hande written 0700" '[ -f "$RUN" ] && [ "$(stat -c %a "$RUN")" = 700 ]'
ok "run-hande: channel hande, push off, nudge off" 'grep -q "CCC_CHANNEL=hande" "$RUN" && grep -q "CCC_PUSH_ENABLED=false" "$RUN" && grep -q "CCC_WEBHOOK_NUDGE_ENABLED=false" "$RUN"'
ok "run-hande: data/log/session paths under .ccc-hande" 'grep -q "BOT_DATA_DIR=$H/.ccc-hande " "$RUN" && grep -q "LOGS_DIR=$H/.ccc-hande/logs " "$RUN" && grep -q "SESSION_STORE_PATH=$H/.ccc-hande/sessions.json " "$RUN"'
ok "run-hande: hande config + socket replace the Matrix config token" 'grep -q "CCC_HANDE_CONFIG_PATH=$CFG CCC_HANDE_SOCKET_PATH=$P/var/run/ccc-hande/responder.sock" "$RUN"'
ok "run-hande: no Matrix-only token left" '! grep -q "CCC_MATRIX_" "$RUN" && ! grep -q "\.ccc-matrix" "$RUN" && ! grep -q "CCC_PUSH_SPOOL" "$RUN" && ! grep -q "NUDGE_ENV\|RELAY_ENV" "$RUN"'
ok "run-hande: relay vars defined empty before exec (set -u safe)" 'grep -q "^CCC_PUSH_FLEET_RELAY_URL=\"\"; CCC_PUSH_FLEET_RELAY_SECRET_FILE=\"\"; CCC_PUSH_FLEET_NODE=\"\"; CCC_PUSH_FLEET_RELAY_DIRECT_EVENTS=\"\"" "$RUN"'
ok "run-hande: generation resolution, cd, set -eu kept" 'grep -q "bridge-current.sh --print-source" "$RUN" && grep -q "^cd \"\$CCC_SRC/bridge\"$" "$RUN" && grep -q "^set -eu$" "$RUN" && grep -q "telegram_bot --path $H$" "$RUN"'
ok "run-hande: provenance comment" 'grep -q "cloned from run-matrix by ccc-node scripts/install-hande-bridge-termux.sh" "$RUN"'
ok "run-hande is a valid sh script" 'bash -n "$RUN"'
ok "config written 0600 with socket_mode 0600 and no socket_group" '[ "$(stat -c %a "$CFG")" = 600 ] && python3 - "$CFG" <<'"'"'PY'"'"'
import json, sys
d = json.load(open(sys.argv[1]))
assert d["account"] == "@nodea:hande" and d["owner"] == "@owner:hande" and d["rooms"] == ["nodea"]
assert d["family_users"] == ["@kid:hande"] and d["socket_mode"] == "0600" and "socket_group" not in d
assert d["socket_path"].endswith("/var/run/ccc-hande/responder.sock") and d["steward_tools"] is True
PY'
ok "runit service dir: run execs run-hande, finish, log/run" 'grep -qx "exec $RUN" "$SVC/run" && [ -x "$SVC/run" ] && [ -x "$SVC/finish" ] && grep -q "svlogd -tt $H/.ccc-hande/logs" "$SVC/log/run"'
ok "no down file on a normal install; sv up + status called" '[ ! -e "$SVC/down" ] && grep -q "^sv up ccc-hande-bridge$" "$CALLS" && grep -q "^sv status ccc-hande-bridge$" "$CALLS"'
ok "socket dir and data dirs created 0700" '[ "$(stat -c %a "$P/var/run/ccc-hande")" = 700 ] && [ -d "$H/.ccc-hande/logs" ]'
ok "frontends gained hande (matrix kept)" 'grep -qx hande "$CLAUDE/frontends" && grep -qx matrix "$CLAUDE/frontends"'
ok "report names sv status, next step and rollback" 'grep -q "^sv: run: ccc-hande-bridge" <<<"$out" && grep -q "next:" <<<"$out" && grep -q "rollback:" <<<"$out"'

# ---- idempotent second run (the hand-port's empty-defs line is also absorbed) -----------
out2="$(run_install --node nodea --family-user @kid:hande 2>&1)"; rc=$?
ok "second run exits 0 and does not duplicate the frontends line" '[ "$rc" = 0 ] && [ "$(grep -cx hande "$CLAUDE/frontends")" = 1 ]'
ok "second run reproduces the same run-hande" 'cmp -s "$RUN" <(run_install --node nodea --family-user @kid:hande >/dev/null 2>&1; cat "$RUN")'

# ---- --no-start / --room -----------------------------------------------------------------
: > "$CALLS"
out3="$(run_install --node nodeb --room agent --no-start 2>&1)"; rc=$?
ok "--no-start writes a down file and calls no sv" '[ "$rc" = 0 ] && [ -f "$SVC/down" ] && ! grep -q "^sv up" "$CALLS"'
ok "--room overrides the room name" 'python3 -c "import json,sys; d=json.load(open(sys.argv[1])); assert d[\"rooms\"]==[\"agent\"] and d[\"account\"]==\"@nodeb:hande\"" "$CFG"'
out4="$(run_install --node nodeb --room agent 2>&1)"; rc=$?
ok "a later normal run removes the down file" '[ "$rc" = 0 ] && [ ! -e "$SVC/down" ] && grep -q "removed $SVC/down" <<<"$out4"'

# ---- a launcher without the config token is refused before anything is written ----------
sed -i 's/CCC_MATRIX_CONFIG_PATH=[^ ]* //' "$H/.local/share/ccc-matrix/run-matrix"
out5="$(run_install --node nodec --dry-run 2>&1)"; rc=$?
ok "run-matrix without CCC_MATRIX_CONFIG_PATH is refused" '[ "$rc" != 0 ] && grep -q "no CCC_MATRIX_CONFIG_PATH token" <<<"$out5"'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
