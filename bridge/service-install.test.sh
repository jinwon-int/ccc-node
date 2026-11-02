#!/usr/bin/env bash
# Tests for bridge/service-launchd.sh + bridge/service-systemd.sh and the
# start.sh --install/--uninstall dispatch (#584 P3-2). Hermetic: fake HOME,
# fake CCC_SYSTEMD_DIR, stubbed systemctl/launchctl — no real launchd or
# systemd contact, no bridge processes started.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
SLD="$HERE/service-launchd.sh"
SSD="$HERE/service-systemd.sh"
START="$HERE/start.sh"
pass=0; fail=0
ok()  { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }
okc() { if [ "$1" = "$2" ]; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $3 (rc=$1 want=$2)"; fi; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# Fixture stubs name the build host's resolved bash, not `#!/usr/bin/env bash`
# which cannot resolve on Termux (#1153; same root as #472/#663).
. "$(cd "$(dirname "$0")/.." && pwd)/claude/hooks/lib/test-stub.sh"

# Stub systemctl / launchctl: record every invocation, always succeed.
SC_CALLS="$TMP/systemctl.calls"
SC_STUB="$TMP/systemctl"
write_exec_stub "$SC_STUB" <<EOF
echo "\$*" >> "$SC_CALLS"
exit 0
EOF
LC_CALLS="$TMP/launchctl.calls"
LC_STUB="$TMP/launchctl"
write_exec_stub "$LC_STUB" <<EOF
echo "\$*" >> "$LC_CALLS"
exit 0
EOF

# Fixed MemTotal (4000000 kB = 4096000000 B) so percent limits resolve to
# known bytes: 50% -> 2048000000, 75% -> 3072000000 (page-aligned already).
printf 'MemTotal:        4000000 kB\nMemFree:          100000 kB\n' > "$TMP/meminfo"
export CCC_MEMINFO_FILE="$TMP/meminfo"

PROJECT="$TMP/myproj"
mkdir -p "$PROJECT"
FH="$TMP/home"
mkdir -p "$FH"

# Unit scope follows the euid running the test (root => system scope).
if [ "$(id -u)" = "0" ]; then
    WANTED="multi-user.target"
    DAEMON_RELOAD="daemon-reload"
else
    # shellcheck disable=SC2034  # WANTED is read via eval inside ok()
    WANTED="default.target"
    # shellcheck disable=SC2034  # DAEMON_RELOAD is read via eval inside ok()
    DAEMON_RELOAD="--user daemon-reload"
fi

OUT="$TMP/out"; RC=0
run() { RC=0; "$@" >"$OUT" 2>&1 || RC=$?; }

# ---- systemd: unit generation content --------------------------------------
SD="$TMP/sd"
UNIT="$SD/ccc-telegram-bridge.service"
run env HOME="$FH" CCC_SYSTEMD_DIR="$SD" CCC_SYSTEMCTL="$SC_STUB" \
    bash "$SSD" install --project-root "$PROJECT"
okc "$RC" 0 "systemd install exits 0"
ok "unit file written into CCC_SYSTEMD_DIR" '[ -f "$UNIT" ]'
ok "unit ExecStart runs start.sh with project path" \
   'grep -Fxq "ExecStart=/bin/bash $HERE/start.sh --path $PROJECT" "$UNIT"'
ok "unit restart policy is always"      'grep -Fxq "Restart=always" "$UNIT"'
ok "unit restart delay is 3s"           'grep -Fxq "RestartSec=3" "$UNIT"'
ok "unit gives TERM only to the bridge during drain" \
   'grep -Fxq "KillMode=mixed" "$UNIT"'
ok "unit keeps bounded whole-cgroup force cleanup" \
   'grep -Fxq "SendSIGKILL=yes" "$UNIT" && grep -Fxq "TimeoutStopSec=70" "$UNIT"'
ok "unit WorkingDirectory is repo root" 'grep -Fxq "WorkingDirectory=$REPO" "$UNIT"'
ok "unit WantedBy matches scope"        'grep -Fxq "WantedBy=$WANTED" "$UNIT"'
ok "unit has no proxy env when unset"   '! grep -q "http_proxy" "$UNIT"'
ok "unit has no blank lines (sed collapse)" '! grep -q "^$" "$UNIT"'
ok "unit reads the shared owner-only bridge EnvironmentFile (#1771)" \
   'grep -Fxq "EnvironmentFile=-$FH/.config/ccc-node/bridge.env" "$UNIT"'
ok "unit carries no provider secret in an Environment= line" \
   '! grep -Eq "^Environment=(CLAUDE_CODE_OAUTH_TOKEN|ANTHROPIC_)" "$UNIT"'
ok "systemd install ran daemon-reload"  'grep -q "daemon-reload" "$SC_CALLS"'
ok "systemd install enabled --now the service" \
   'grep -q "enable --now ccc-telegram-bridge.service" "$SC_CALLS"'
# shellcheck disable=SC2034  # GUARD is read via eval inside ok()
GUARD="$UNIT.d/10-ccc-memory-guard.conf"
ok "install writes the memory-guard drop-in (#1877)" '[ -f "$GUARD" ]'
ok "memory guard defaults to MemoryHigh=50% MemoryMax=75% OOMPolicy=continue" \
   'grep -Fxq "[Service]" "$GUARD" && grep -Fxq "MemoryHigh=50%" "$GUARD" && grep -Fxq "MemoryMax=75%" "$GUARD" && grep -Fxq "OOMPolicy=continue" "$GUARD"'
ok "memory guard drop-in carries the generator marker" \
   'head -n 1 "$GUARD" | grep -q "^# ccc-node bridge memory guard (#1877)"'
ok "memory guard stays out of the canonical main unit" \
   '! grep -Eq "^(MemoryHigh|MemoryMax|OOMPolicy)=" "$UNIT"'

# ---- systemd: setup/self-update reconciliation -----------------------------
# Identical canonical content is a true no-op: no inode replacement, reload,
# enable, or restart.
: > "$SC_CALLS"
# shellcheck disable=SC2034  # unit_inode_before is read via eval inside ok()
unit_inode_before="$(stat -c %i "$UNIT")"
# shellcheck disable=SC2034  # unit_mtime_before is read via eval inside ok()
unit_mtime_before="$(stat -c %Y "$UNIT")"
run env HOME="$FH" CCC_SYSTEMD_DIR="$SD" CCC_SYSTEMCTL="$SC_STUB" \
    bash "$SSD" reconcile
okc "$RC" 0 "identical systemd reconcile exits 0"
ok "identical reconcile reports untouched canonical unit" \
   'grep -q "already canonical; left untouched" "$OUT"'
ok "identical reconcile preserves inode and mtime" \
   '[ "$(stat -c %i "$UNIT")" = "$unit_inode_before" ] && [ "$(stat -c %Y "$UNIT")" = "$unit_mtime_before" ]'
ok "identical reconcile does not contact systemctl" '[ ! -s "$SC_CALLS" ]'

# Drift is replaced with canonical bytes and only daemon-reloaded. A drop-in is
# node-local policy and must remain byte-for-byte untouched.
sed -i 's/^Restart=always$/Restart=on-failure/' "$UNIT"
mkdir -p "$UNIT.d"
printf '[Service]\nEnvironment=CCC_LOCAL_OVERRIDE=true\n' > "$UNIT.d/override.conf"
# shellcheck disable=SC2034  # dropin_before is read via eval inside ok()
dropin_before="$(sha256sum "$UNIT.d/override.conf")"
: > "$SC_CALLS"
run env HOME="$FH" CCC_SYSTEMD_DIR="$SD" CCC_SYSTEMCTL="$SC_STUB" \
    bash "$SSD" reconcile
okc "$RC" 0 "drifted systemd reconcile exits 0"
ok "drifted unit is restored to canonical Restart policy" \
   'grep -Fxq "Restart=always" "$UNIT" && ! grep -q "Restart=on-failure" "$UNIT"'
ok "reconcile performs only daemon-reload (no session disruption)" \
   '[ "$(cat "$SC_CALLS")" = "$DAEMON_RELOAD" ]'
ok "reconcile preserves node-local drop-ins" \
   '[ "$(sha256sum "$UNIT.d/override.conf")" = "$dropin_before" ]'

# Units rendered before the drain contract have neither KillMode nor an
# explicit SendSIGKILL. They are still recognized and upgraded in place.
sed -i '/^KillMode=mixed$/d; /^SendSIGKILL=yes$/d; s/^TimeoutStopSec=70$/TimeoutStopSec=20/' "$UNIT"
: > "$SC_CALLS"
run env HOME="$FH" CCC_SYSTEMD_DIR="$SD" CCC_SYSTEMCTL="$SC_STUB" \
    bash "$SSD" reconcile
okc "$RC" 0 "pre-drain generated unit reconciles"
ok "pre-drain unit gains bounded mixed cgroup lifecycle" \
   'grep -Fxq "KillMode=mixed" "$UNIT" && grep -Fxq "SendSIGKILL=yes" "$UNIT" && grep -Fxq "TimeoutStopSec=70" "$UNIT"'
ok "pre-drain reconciliation only daemon-reloads" \
   '[ "$(cat "$SC_CALLS")" = "$DAEMON_RELOAD" ]'

# Units rendered before #1771 have no shared EnvironmentFile. They are still
# recognized and gain it in place (optional "-" path: inert until placed).
sed -i '/^EnvironmentFile=/d; /^# Shared provider\/auth env/d; /^# 0600 (#1771)/d' "$UNIT"
: > "$SC_CALLS"
run env HOME="$FH" CCC_SYSTEMD_DIR="$SD" CCC_SYSTEMCTL="$SC_STUB" \
    bash "$SSD" reconcile
okc "$RC" 0 "pre-#1771 generated unit reconciles"
ok "pre-#1771 unit gains the shared EnvironmentFile" \
   'grep -Fxq "EnvironmentFile=-$FH/.config/ccc-node/bridge.env" "$UNIT"'
ok "pre-#1771 reconciliation only daemon-reloads" \
   '[ "$(cat "$SC_CALLS")" = "$DAEMON_RELOAD" ]'

# Any other EnvironmentFile is node-local policy: the unit is bespoke and left
# untouched rather than silently dropping the operator's file.
sed -i 's|^EnvironmentFile=.*|EnvironmentFile=/etc/ccc-local/other.env|' "$UNIT"
# shellcheck disable=SC2034  # foreign_envfile_before is read via eval inside ok()
foreign_envfile_before="$(sha256sum "$UNIT")"
: > "$SC_CALLS"
run env HOME="$FH" CCC_SYSTEMD_DIR="$SD" CCC_SYSTEMCTL="$SC_STUB" \
    bash "$SSD" reconcile
okc "$RC" 0 "foreign EnvironmentFile unit is a bounded skip"
ok "foreign EnvironmentFile unit stays untouched" \
   '[ "$(sha256sum "$UNIT")" = "$foreign_envfile_before" ] && [ ! -s "$SC_CALLS" ]'
sed -i "s|^EnvironmentFile=.*|EnvironmentFile=-$FH/.config/ccc-node/bridge.env|" "$UNIT"

# Dry-run compares the same renderer but cannot mutate the main unit/drop-in or
# contact systemctl.
sed -i 's/^Restart=always$/Restart=on-failure/' "$UNIT"
unit_before="$(sha256sum "$UNIT")"
: > "$SC_CALLS"
run env HOME="$FH" CCC_SYSTEMD_DIR="$SD" CCC_SYSTEMCTL="$SC_STUB" \
    bash "$SSD" reconcile --dry-run
okc "$RC" 0 "systemd reconcile dry-run exits 0"
ok "dry-run explicitly reports atomic replacement without restart" \
   'grep -q "would atomically replace" "$OUT" && grep -q "no stop, start, enable, or restart" "$OUT"'
ok "dry-run is mutation-free" \
   '[ "$(sha256sum "$UNIT")" = "$unit_before" ] && [ "$(sha256sum "$UNIT.d/override.conf")" = "$dropin_before" ] && [ ! -s "$SC_CALLS" ]'

# If daemon-reload fails after the atomic replacement, restore the exact old
# bytes and reload them. The service itself is never restarted.
ROLLBACK_CALLS="$TMP/systemctl-rollback.calls"
ROLLBACK_COUNT="$TMP/systemctl-rollback.count"
ROLLBACK_STUB="$TMP/systemctl-rollback"
write_exec_stub "$ROLLBACK_STUB" <<SH
echo "\$*" >> "$ROLLBACK_CALLS"
count="\$(cat "$ROLLBACK_COUNT" 2>/dev/null || echo 0)"
count=\$((count + 1))
printf '%s' "\$count" > "$ROLLBACK_COUNT"
[ "\$count" = 1 ] && exit 1
exit 0
SH
unit_before="$(sha256sum "$UNIT")"
run env HOME="$FH" CCC_SYSTEMD_DIR="$SD" CCC_SYSTEMCTL="$ROLLBACK_STUB" \
    bash "$SSD" reconcile
okc "$RC" 1 "reload failure makes reconcile fail closed"
ok "reload failure restores exact previous main-unit bytes" \
   '[ "$(sha256sum "$UNIT")" = "$unit_before" ]'
ok "rollback reloads the restored definition without restart" \
   '[ "$(grep -Fxc -- "$DAEMON_RELOAD" "$ROLLBACK_CALLS")" = 2 ] && ! grep -Eq "restart|start|stop|enable" "$ROLLBACK_CALLS"'
ok "successful rollback leaves no staging artifacts" \
   '! compgen -G "$SD/.ccc-telegram-bridge.service.*" >/dev/null'

# A failed rollback reload is also explicit and fail-closed: the old file is
# still restored, no service lifecycle command is attempted, and the caller
# gets a non-zero result for operator follow-up.
ALWAYS_FAIL_CALLS="$TMP/systemctl-always-fail.calls"
ALWAYS_FAIL_STUB="$TMP/systemctl-always-fail"
write_exec_stub "$ALWAYS_FAIL_STUB" <<SH
echo "\$*" >> "$ALWAYS_FAIL_CALLS"
exit 1
SH
# shellcheck disable=SC2034  # unit_before is read via eval inside ok()
unit_before="$(sha256sum "$UNIT")"
run env HOME="$FH" CCC_SYSTEMD_DIR="$SD" CCC_SYSTEMCTL="$ALWAYS_FAIL_STUB" \
    bash "$SSD" reconcile
okc "$RC" 1 "rollback reload failure remains fail closed"
ok "rollback reload failure still restores exact previous bytes" \
   '[ "$(sha256sum "$UNIT")" = "$unit_before" ]'
ok "rollback reload failure is explicit and never restarts" \
   '[ "$(grep -Fxc -- "$DAEMON_RELOAD" "$ALWAYS_FAIL_CALLS")" = 2 ] && grep -q "rollback daemon-reload also failed" "$OUT" && ! grep -Eq "restart|start|stop|enable" "$ALWAYS_FAIL_CALLS"'

# Arbitrary legacy directives are not smuggled into the canonical main unit.
# The bespoke unit is left for #831's explicit renderer-flag disposition.
printf '%s\n' 'Environment=CCC_TELEGRAM_READABLE_RENDERER=true' >> "$UNIT"
# shellcheck disable=SC2034  # bespoke_before is read via eval inside ok()
bespoke_before="$(sha256sum "$UNIT")"
: > "$SC_CALLS"
run env HOME="$FH" CCC_SYSTEMD_DIR="$SD" CCC_SYSTEMCTL="$SC_STUB" \
    bash "$SSD" reconcile
okc "$RC" 0 "bespoke systemd main unit is a bounded skip"
ok "bespoke main unit stays untouched and points overrides to drop-ins" \
   '[ "$(sha256sum "$UNIT")" = "$bespoke_before" ] && grep -q "drop-ins" "$OUT"'
ok "bespoke skip does not contact systemctl" '[ ! -s "$SC_CALLS" ]'
sed -i '/^Environment=CCC_TELEGRAM_READABLE_RENDERER=true$/d' "$UNIT"
sed -i 's/^Restart=on-failure$/Restart=always/' "$UNIT"

# Noncanonical filesystem topology is never normalized implicitly. A symlinked
# main unit keeps both its directory entry and target intact.
SYMLINK_SD="$TMP/symlink-sd"
SYMLINK_TARGET="$TMP/symlink-target.service"
mkdir -p "$SYMLINK_SD"
cp "$UNIT" "$SYMLINK_TARGET"
sed -i 's/^Restart=always$/Restart=on-failure/' "$SYMLINK_TARGET"
SYMLINK_UNIT="$SYMLINK_SD/ccc-telegram-bridge.service"
ln -s "$SYMLINK_TARGET" "$SYMLINK_UNIT"
# shellcheck disable=SC2034  # symlink_inode_before is read via eval inside ok()
symlink_inode_before="$(stat -c %i "$SYMLINK_UNIT")"
# shellcheck disable=SC2034  # symlink_target_before is read via eval inside ok()
symlink_target_before="$(readlink "$SYMLINK_UNIT")"
# shellcheck disable=SC2034  # symlink_bytes_before is read via eval inside ok()
symlink_bytes_before="$(sha256sum "$SYMLINK_TARGET")"
: > "$SC_CALLS"
run env HOME="$FH" CCC_SYSTEMD_DIR="$SYMLINK_SD" CCC_SYSTEMCTL="$SC_STUB" \
    bash "$SSD" reconcile
okc "$RC" 0 "symlinked systemd main unit is a bounded skip"
ok "symlink reconcile preserves link identity and target bytes" \
   '[ -L "$SYMLINK_UNIT" ] && [ "$(stat -c %i "$SYMLINK_UNIT")" = "$symlink_inode_before" ] && [ "$(readlink "$SYMLINK_UNIT")" = "$symlink_target_before" ] && [ "$(sha256sum "$SYMLINK_TARGET")" = "$symlink_bytes_before" ]'
ok "symlink skip directs explicit normalization and drop-ins" \
   'grep -q "Normalize the main-unit path explicitly" "$OUT" && grep -q "drop-ins" "$OUT"'
ok "symlink skip does not contact systemctl" '[ ! -s "$SC_CALLS" ]'

# A multiply hard-linked main unit likewise keeps all names, bytes, and link
# metadata intact instead of severing the topology with an atomic replacement.
# The fixture itself needs filesystem hard-link support: Android/Termux's
# f2fs refuses `ln` with EPERM even for the owner (실측 gongyung), and on such
# a filesystem a multiply-linked unit can never exist, so the reconcile path
# this block exercises is unreachable there — probe once and skip visibly.
HARDLINK_SD="$TMP/hardlink-sd"
HARDLINK_PEER="$TMP/hardlink-peer.service"
mkdir -p "$HARDLINK_SD"
hl_probe="$TMP/hardlink-probe"; printf 'x' > "$hl_probe"
if ln "$hl_probe" "$hl_probe.link" 2>/dev/null; then
rm -f "$hl_probe" "$hl_probe.link"
cp "$UNIT" "$HARDLINK_PEER"
sed -i 's/^Restart=always$/Restart=on-failure/' "$HARDLINK_PEER"
HARDLINK_UNIT="$HARDLINK_SD/ccc-telegram-bridge.service"
ln "$HARDLINK_PEER" "$HARDLINK_UNIT"
# shellcheck disable=SC2034  # hardlink_inode_before is read via eval inside ok()
hardlink_inode_before="$(stat -c %i "$HARDLINK_UNIT")"
# shellcheck disable=SC2034  # hardlink_count_before is read via eval inside ok()
hardlink_count_before="$(stat -c %h "$HARDLINK_UNIT")"
# shellcheck disable=SC2034  # hardlink_bytes_before is read via eval inside ok()
hardlink_bytes_before="$(sha256sum "$HARDLINK_UNIT" | cut -d' ' -f1)"
: > "$SC_CALLS"
run env HOME="$FH" CCC_SYSTEMD_DIR="$HARDLINK_SD" CCC_SYSTEMCTL="$SC_STUB" \
    bash "$SSD" reconcile
okc "$RC" 0 "multiply hard-linked systemd main unit is a bounded skip"
ok "hard-link reconcile preserves bytes, identity, and link count" \
   '[ "$(sha256sum "$HARDLINK_UNIT" | cut -d" " -f1)" = "$hardlink_bytes_before" ] && [ "$(sha256sum "$HARDLINK_PEER" | cut -d" " -f1)" = "$hardlink_bytes_before" ] && [ "$(stat -c %i "$HARDLINK_UNIT")" = "$hardlink_inode_before" ] && [ "$(stat -c %i "$HARDLINK_PEER")" = "$hardlink_inode_before" ] && [ "$(stat -c %h "$HARDLINK_UNIT")" = "$hardlink_count_before" ]'
ok "hard-link skip directs explicit normalization and drop-ins" \
   'grep -q "Normalize the main-unit path explicitly" "$OUT" && grep -q "drop-ins" "$OUT"'
ok "hard-link skip does not contact systemctl" '[ ! -s "$SC_CALLS" ]'
else
rm -f "$hl_probe"
echo "SKIP: hard-link reconcile cases (filesystem denies hard links on this host)"
fi

# User scope uses the user target and --user daemon-reload, while retaining the
# same state-preserving reconciliation behavior. The scope override is accepted
# only alongside the hermetic directory seam.
USD="$TMP/user-sd"
run env HOME="$FH" CCC_SYSTEMD_DIR="$USD" CCC_SYSTEMD_SCOPE=user CCC_SYSTEMCTL="$SC_STUB" \
    bash "$SSD" install --project-root "$PROJECT"
okc "$RC" 0 "user-scope systemd install exits 0"
UUNIT="$USD/ccc-telegram-bridge.service"
ok "user-scope unit targets default.target" 'grep -Fxq "WantedBy=default.target" "$UUNIT"'
sed -i 's/^Restart=always$/Restart=on-failure/' "$UUNIT"
: > "$SC_CALLS"
run env HOME="$FH" CCC_SYSTEMD_DIR="$USD" CCC_SYSTEMD_SCOPE=user CCC_SYSTEMCTL="$SC_STUB" \
    bash "$SSD" reconcile
okc "$RC" 0 "user-scope reconcile exits 0"
ok "user-scope reconcile only invokes --user daemon-reload" \
   '[ "$(cat "$SC_CALLS")" = "--user daemon-reload" ] && grep -Fxq "Restart=always" "$UUNIT"'

# The root/system scope uses multi-user.target and the system manager (no
# --user flag), independent of the account executing this hermetic test.
RSD="$TMP/root-sd"
run env HOME="$FH" CCC_SYSTEMD_DIR="$RSD" CCC_SYSTEMD_SCOPE=system CCC_SYSTEMCTL="$SC_STUB" \
    bash "$SSD" install --project-root "$PROJECT"
okc "$RC" 0 "root/system-scope systemd install exits 0"
RUNIT="$RSD/ccc-telegram-bridge.service"
ok "root/system-scope unit targets multi-user.target" \
   'grep -Fxq "WantedBy=multi-user.target" "$RUNIT"'
sed -i 's/^Restart=always$/Restart=on-failure/' "$RUNIT"
: > "$SC_CALLS"
run env HOME="$FH" CCC_SYSTEMD_DIR="$RSD" CCC_SYSTEMD_SCOPE=system CCC_SYSTEMCTL="$SC_STUB" \
    bash "$SSD" reconcile
okc "$RC" 0 "root/system-scope reconcile exits 0"
ok "root/system-scope reconcile invokes only system daemon-reload" \
   '[ "$(cat "$SC_CALLS")" = "daemon-reload" ] && grep -Fxq "Restart=always" "$RUNIT"'

# ---- systemd: proxy propagation --------------------------------------------
SD2="$TMP/sd-proxy"
run env HOME="$FH" CCC_SYSTEMD_DIR="$SD2" CCC_SYSTEMCTL="$SC_STUB" \
    bash "$SSD" install --project-root "$PROJECT" --proxy-url "http://127.0.0.1:3128"
okc "$RC" 0 "systemd proxy install exits 0"
ok "proxy unit carries https_proxy" \
   'grep -Fxq "Environment=https_proxy=http://127.0.0.1:3128" "$SD2/ccc-telegram-bridge.service"'
ok "proxy unit carries no_proxy" \
   'grep -q "^Environment=no_proxy=localhost,127.0.0.1," "$SD2/ccc-telegram-bridge.service"'

# ---- systemd: BRIDGE_SERVICE_NAME override ---------------------------------
run env HOME="$FH" CCC_SYSTEMD_DIR="$SD" CCC_SYSTEMCTL="$SC_STUB" BRIDGE_SERVICE_NAME="ccc-telegram-bridge-alt" \
    bash "$SSD" install --project-root "$PROJECT"
okc "$RC" 0 "named install exits 0"
ok "BRIDGE_SERVICE_NAME picks the unit filename" '[ -f "$SD/ccc-telegram-bridge-alt.service" ]'

# ---- systemd: idempotency + uninstall --------------------------------------
# shellcheck disable=SC2034  # consumed via eval in ok()
before="$(cat "$UNIT")"
run env HOME="$FH" CCC_SYSTEMD_DIR="$SD" CCC_SYSTEMCTL="$SC_STUB" \
    bash "$SSD" install --project-root "$PROJECT"
okc "$RC" 0 "systemd re-install exits 0"
ok "re-install keeps identical unit content" '[ "$before" = "$(cat "$UNIT")" ]'

: > "$SC_CALLS"
run env HOME="$FH" CCC_SYSTEMD_DIR="$SD" CCC_SYSTEMCTL="$SC_STUB" bash "$SSD" uninstall
okc "$RC" 0 "systemd uninstall exits 0"
ok "uninstall removed the unit file" '[ ! -f "$UNIT" ]'
ok "uninstall disabled --now the service" \
   'grep -q "disable --now ccc-telegram-bridge.service" "$SC_CALLS"'
ok "uninstall ran daemon-reload" 'grep -q "daemon-reload" "$SC_CALLS"'
ok "uninstall removes the generated memory-guard drop-in" '[ ! -e "$GUARD" ]'
ok "uninstall keeps operator drop-ins" '[ -f "$UNIT.d/override.conf" ]'
run env HOME="$FH" CCC_SYSTEMD_DIR="$SD" CCC_SYSTEMCTL="$SC_STUB" bash "$SSD" uninstall
okc "$RC" 0 "second uninstall exits 0 (idempotent)"
ok "second uninstall reports not installed" 'grep -q "not installed" "$OUT"'

# ---- systemd: memory guard overrides + subcommand (#1877) -------------------
SDG="$TMP/sd-guard"
UG="$SDG/ccc-telegram-bridge.service"
GG="$UG.d/10-ccc-memory-guard.conf"
run env HOME="$FH" CCC_SYSTEMD_DIR="$SDG" CCC_SYSTEMCTL="$SC_STUB" \
    CCC_BRIDGE_MEMORY_HIGH=8G CCC_BRIDGE_MEMORY_MAX=12G \
    bash "$SSD" install --project-root "$PROJECT"
okc "$RC" 0 "install with memory overrides exits 0"
ok "memory overrides land in the drop-in" \
   'grep -Fxq "MemoryHigh=8G" "$GG" && grep -Fxq "MemoryMax=12G" "$GG" && grep -Fxq "OOMPolicy=continue" "$GG"'
# Reinstall without the env keeps the per-node values (#2633).
run env HOME="$FH" CCC_SYSTEMD_DIR="$SDG" CCC_SYSTEMCTL="$SC_STUB" \
    bash "$SSD" install --project-root "$PROJECT"
okc "$RC" 0 "reinstall without memory env exits 0 (#2633)"
ok "reinstall without memory env keeps the per-node drop-in values (#2633)" \
   'grep -Fxq "MemoryHigh=8G" "$GG" && grep -Fxq "MemoryMax=12G" "$GG" && grep -q "keeping the existing memory guard value(s): MemoryHigh=8G MemoryMax=12G" "$OUT"'

rm -rf "$SDG"
run env HOME="$FH" CCC_SYSTEMD_DIR="$SDG" CCC_SYSTEMCTL="$SC_STUB" CCC_BRIDGE_MEMORY_GUARD=0 \
    bash "$SSD" install --project-root "$PROJECT"
okc "$RC" 0 "install with CCC_BRIDGE_MEMORY_GUARD=0 exits 0"
ok "CCC_BRIDGE_MEMORY_GUARD=0 installs the unit without the drop-in" '[ -f "$UG" ] && [ ! -e "$GG" ]'

for bad in "HIGH=abc" "MAX=0%" "MAX=101%" "HIGH=80% MAX=60%" "HIGH=16G MAX=8G" "HIGH=infinity MAX=75%"; do
    rm -rf "$SDG"
    # shellcheck disable=SC2046  # intentional word splitting into env assignments
    run env HOME="$FH" CCC_SYSTEMD_DIR="$SDG" CCC_SYSTEMCTL="$SC_STUB" \
        $(for kv in $bad; do printf 'CCC_BRIDGE_MEMORY_%s ' "$kv"; done) \
        bash "$SSD" install --project-root "$PROJECT"
    okc "$RC" 1 "invalid memory guard ($bad) is refused"
    ok "invalid memory guard ($bad) writes nothing" '[ ! -e "$UG" ]'
done

# Stateful systemctl stub for memory-guard (#2545): is-active follows
# $MG/active; `show -p ControlGroup` reports a fake cgroup under $MG/cg;
# set-property --runtime writes the kernel files the way the real one would
# (infinity -> max) unless $MG/noapply exists (models the half-applied host).
MG="$TMP/mg"
MG_CALLS="$MG/calls"
MG_STUB="$TMP/systemctl-mg"
MG_CG="/system.slice/ccc-telegram-bridge.service"
mkdir -p "$MG/cg$MG_CG"
write_exec_stub "$MG_STUB" <<EOF
echo "\$*" >> "$MG_CALLS"
[ "\$1" = "--user" ] && shift
case "\$1" in
    is-active) [ -e "$MG/active" ]; exit ;;
    show)
        if [ -e "$MG/active" ]; then st=active; cg="$MG_CG"; else st=inactive; cg=""; fi
        case "\$*" in
            *--value*ControlGroup*|*ControlGroup*--value*) echo "\$cg" ;;
            *--value*MemoryMax*|*MemoryMax*--value*) cat "$MG/loaded-max" 2>/dev/null || echo infinity ;;
            *) printf 'ActiveState=%s\nControlGroup=%s\n' "\$st" "\$cg" ;;
        esac
        exit 0 ;;
    set-property)
        [ -e "$MG/noapply" ] && exit 0
        for kv in "\$@"; do
            case "\$kv" in
                MemoryHigh=*) v="\${kv#*=}"; [ "\$v" = infinity ] && v=max; echo "\$v" > "$MG/cg$MG_CG/memory.high" ;;
                MemoryMax=*) v="\${kv#*=}"; [ "\$v" = infinity ] && v=max; echo "\$v" > "$MG/cg$MG_CG/memory.max" ;;
            esac
        done
        exit 0 ;;
esac
exit 0
EOF
mg_reset() {
    # $1 = active|inactive; cgroup back to the unlimited 'max' state.
    rm -f "$MG/active" "$MG/noapply" "$MG/loaded-max"
    [ "$1" = "active" ] && : > "$MG/active"
    echo max > "$MG/cg$MG_CG/memory.high"
    echo max > "$MG/cg$MG_CG/memory.max"
    : > "$MG_CALLS"
}
mg_run() {
    # System scope pinned so the expected call lines do not depend on the euid
    # running the suite (CI is non-root); the user-scope case overrides it.
    run env HOME="$FH" CCC_SYSTEMD_DIR="$SDG" CCC_SYSTEMD_SCOPE=system CCC_SYSTEMCTL="$MG_STUB" \
        CCC_CGROUP_ROOT="$MG/cg" "$@"
}
# shellcheck disable=SC2034  # read via eval inside ok()
RESTARTS='(^|--user )(stop|start|restart|try-restart|reload-or-restart|kill) '
# shellcheck disable=SC2034  # read via eval inside ok()
SETPROP_DEFAULT="set-property --runtime ccc-telegram-bridge.service MemoryHigh=2048000000 MemoryMax=3072000000"

# memory-guard on an existing unit (e.g. installed before #1877): dry-run is
# read-only; the real run writes the drop-in, daemon-reloads, and — on an
# INACTIVE unit — changes nothing at runtime.
rm -rf "$SDG"
run env HOME="$FH" CCC_SYSTEMD_DIR="$SDG" CCC_SYSTEMCTL="$SC_STUB" CCC_BRIDGE_MEMORY_GUARD=0 \
    bash "$SSD" install --project-root "$PROJECT"
mg_reset inactive
mg_run bash "$SSD" memory-guard --dry-run
okc "$RC" 0 "memory-guard --dry-run exits 0"
ok "memory-guard --dry-run writes nothing and never calls systemctl" \
   '[ ! -e "$GG" ] && [ ! -s "$MG_CALLS" ] && grep -q "MemoryMax=75%" "$OUT"'
ok "memory-guard --dry-run shows the resolved runtime bytes" \
   'grep -q "MemoryHigh=2048000000 MemoryMax=3072000000" "$OUT"'
mg_run bash "$SSD" memory-guard
okc "$RC" 0 "memory-guard exits 0"
ok "memory-guard writes the drop-in" 'grep -Fxq "OOMPolicy=continue" "$GG"'
ok "memory-guard daemon-reloads" 'grep -qx -- "daemon-reload" "$MG_CALLS"'
ok "memory-guard never stops/starts/restarts the unit" '! grep -Eq "$RESTARTS" "$MG_CALLS"'
ok "inactive unit gets no set-property --runtime (#2545)" \
   '! grep -q "set-property" "$MG_CALLS" && grep -q "not active" "$OUT"'
mg_reset inactive
mg_run bash "$SSD" memory-guard
okc "$RC" 0 "repeated memory-guard exits 0"
ok "repeated memory-guard leaves the drop-in alone and skips daemon-reload" \
   'grep -q "already current" "$OUT" && ! grep -q "daemon-reload" "$MG_CALLS" && ! grep -q "set-property" "$MG_CALLS"'
mg_reset inactive
echo 3072000000 > "$MG/loaded-max"
mg_run bash "$SSD" memory-guard
ok "inactive unit with the drop-in loaded raises no shadow warning" '! grep -q "shadows the drop-in" "$OUT"'
mg_reset inactive
mg_run bash "$SSD" memory-guard
ok "inactive unit whose loaded MemoryMax is infinity warns about a runtime shadow" \
   'grep -q "shadows the drop-in" "$OUT"'

# Active unit (#2545): the drop-in alone leaves the running cgroup at max, so
# the same limits are pushed with set-property --runtime — no restart — and the
# effective cgroup values are printed.
rm -f "$GG"
mg_reset active
mg_run bash "$SSD" memory-guard
okc "$RC" 0 "memory-guard on an active unit exits 0"
ok "active unit gets set-property --runtime with percent resolved to bytes" \
   'grep -qx -- "$SETPROP_DEFAULT" "$MG_CALLS"'
ok "active unit is not restarted" '! grep -Eq "$RESTARTS" "$MG_CALLS"'
ok "set-property follows daemon-reload" \
   '[ "$(grep -nx -- "daemon-reload" "$MG_CALLS" | cut -d: -f1)" -lt "$(grep -n "set-property" "$MG_CALLS" | cut -d: -f1)" ]'
ok "verification prints the effective cgroup memory.high/max" \
   'grep -q "memory.high=2048000000 memory.max=3072000000" "$OUT" && ! grep -q "NOT applied" "$OUT"'
# Drop-in already current (written by a pre-#2545 memory-guard that only
# reloaded) while the live cgroup is still at max: the rerun must fix it.
mg_reset active
mg_run bash "$SSD" memory-guard
okc "$RC" 0 "rerun with a current drop-in on an active unit exits 0"
ok "rerun with a current drop-in still applies the runtime limits" \
   'grep -q "already current" "$OUT" && grep -qx -- "$SETPROP_DEFAULT" "$MG_CALLS" && ! grep -q "daemon-reload" "$MG_CALLS"'
# Half-applied host: set-property "succeeds" but the cgroup stays at max.
mg_reset active
: > "$MG/noapply"
mg_run bash "$SSD" memory-guard
okc "$RC" 1 "cgroup still at max after memory-guard exits 1"
ok "verification reports memory.max=max as NOT applied" \
   'grep -q "memory.high=max memory.max=max" "$OUT" && grep -q "memory.max is still .max. — MemoryMax NOT applied" "$OUT"'
# Byte overrides pass through unchanged (K/M/G/T expanded).
mg_reset active
mg_run env CCC_BRIDGE_MEMORY_HIGH=1G CCC_BRIDGE_MEMORY_MAX=1536M bash "$SSD" memory-guard
okc "$RC" 0 "memory-guard with byte overrides exits 0"
ok "byte overrides reach set-property as bytes" \
   'grep -qx "set-property --runtime ccc-telegram-bridge.service MemoryHigh=1073741824 MemoryMax=1610612736" "$MG_CALLS"'
# Unreadable MemTotal: refuse before writing anything. The byte drop-in above
# would now be kept (#2633), so start from no drop-in to hit the 50% default.
rm -f "$GG"
mg_reset active
mg_run env CCC_MEMINFO_FILE="$TMP/no-meminfo" bash "$SSD" memory-guard
okc "$RC" 1 "percent limit without a readable MemTotal exits 1"
ok "unresolvable limit calls no systemctl" \
   '[ ! -s "$MG_CALLS" ] && grep -q "MemTotal unreadable" "$OUT"'
# User scope drives `systemctl --user set-property --runtime`.
rm -f "$GG"
mg_reset active
mg_run env CCC_SYSTEMD_SCOPE=user bash "$SSD" memory-guard
ok "user-scope memory-guard uses systemctl --user set-property --runtime" \
   'grep -qx -- "--user $SETPROP_DEFAULT" "$MG_CALLS"'

# Per-node limits survive reruns without the env (#2633): they live only in
# the generated drop-in, so install/memory-guard keep each unset value from it
# instead of resetting to 50%/75%; an explicit env value still wins.
rm -f "$GG"
mg_reset active
mg_run env CCC_BRIDGE_MEMORY_HIGH=60% CCC_BRIDGE_MEMORY_MAX=80% bash "$SSD" memory-guard
okc "$RC" 0 "memory-guard with per-node percent values exits 0"
mg_reset active
mg_run bash "$SSD" memory-guard
okc "$RC" 0 "memory-guard rerun without env exits 0 (#2633)"
ok "memory-guard rerun without env keeps the per-node drop-in values (#2633)" \
   'grep -Fxq "MemoryHigh=60%" "$GG" && grep -Fxq "MemoryMax=80%" "$GG" && grep -q "already current" "$OUT"'
ok "memory-guard rerun without env says which values it kept (#2633)" \
   'grep -q "keeping the existing memory guard value(s): MemoryHigh=60% MemoryMax=80%" "$OUT"'
ok "kept percent values reach set-property as bytes (#2633)" \
   'grep -qx "set-property --runtime ccc-telegram-bridge.service MemoryHigh=2457600000 MemoryMax=3276800000" "$MG_CALLS"'
mg_reset active
mg_run env CCC_BRIDGE_MEMORY_MAX=90% bash "$SSD" memory-guard
okc "$RC" 0 "memory-guard with only MAX set exits 0 (#2633)"
ok "an explicit env value wins while the unset one is kept (#2633)" \
   'grep -Fxq "MemoryHigh=60%" "$GG" && grep -Fxq "MemoryMax=90%" "$GG"'
mg_reset active
mg_run env CCC_BRIDGE_MEMORY_HIGH=50% CCC_BRIDGE_MEMORY_MAX=75% bash "$SSD" memory-guard
ok "explicit 50%/75% resets a per-node drop-in (#2633)" \
   'grep -Fxq "MemoryHigh=50%" "$GG" && grep -Fxq "MemoryMax=75%" "$GG" && ! grep -q "keeping the existing" "$OUT"'
mg_reset active
mg_run env CCC_BRIDGE_MEMORY_HIGH=90% bash "$SSD" memory-guard
okc "$RC" 1 "explicit HIGH above the kept MAX is refused (#2633)"
ok "refused mixed explicit/kept values leave the drop-in unchanged (#2633)" \
   'grep -Fxq "MemoryHigh=50%" "$GG" && grep -Fxq "MemoryMax=75%" "$GG"'
# A marker drop-in with an unparseable value is not carried over.
printf '%s\n' "$(head -n 1 "$GG")" "[Service]" "MemoryHigh=lots" "MemoryMax=80%" "OOMPolicy=continue" > "$GG"
mg_reset active
mg_run bash "$SSD" memory-guard
okc "$RC" 0 "memory-guard over an invalid kept value exits 0 (#2633)"
ok "an invalid kept value falls back to the default with a warning (#2633)" \
   'grep -Fxq "MemoryHigh=50%" "$GG" && grep -Fxq "MemoryMax=80%" "$GG" && grep -q "ignoring invalid MemoryHigh=lots" "$OUT"'
# A hand-written (marker-less) file is never read as a source of values.
printf '%s\n' "[Service]" "MemoryHigh=10%" "MemoryMax=20%" > "$GG"
mg_reset active
mg_run bash "$SSD" memory-guard --dry-run
ok "a marker-less drop-in is not a source of kept values (#2633)" \
   'grep -q "MemoryHigh=50%" "$OUT" && ! grep -q "keeping the existing" "$OUT"'
# Leave the default drop-in in place for the --remove cases below.
rm -f "$GG"
mg_reset active
mg_run bash "$SSD" memory-guard
okc "$RC" 0 "default drop-in restored for the rollback cases"

# Rollback mirror (--remove): drop-in removed, daemon-reload, runtime limits
# cleared with infinity on an active unit; nothing at runtime when inactive.
mg_reset active
mg_run bash "$SSD" memory-guard --remove --dry-run
okc "$RC" 0 "memory-guard --remove --dry-run exits 0"
ok "memory-guard --remove --dry-run writes nothing and never calls systemctl" \
   '[ -f "$GG" ] && [ ! -s "$MG_CALLS" ] && grep -q "MemoryHigh=infinity MemoryMax=infinity" "$OUT"'
echo 2048000000 > "$MG/cg$MG_CG/memory.high"; echo 3072000000 > "$MG/cg$MG_CG/memory.max"
mg_run bash "$SSD" memory-guard --remove
okc "$RC" 0 "memory-guard --remove exits 0"
ok "memory-guard --remove deletes the generated drop-in" '[ ! -e "$GG" ]'
ok "memory-guard --remove clears runtime limits on the active unit" \
   'grep -qx "set-property --runtime ccc-telegram-bridge.service MemoryHigh=infinity MemoryMax=infinity" "$MG_CALLS" && grep -qx -- "daemon-reload" "$MG_CALLS"'
ok "memory-guard --remove leaves the cgroup at max and does not restart" \
   'grep -q "memory.high=max memory.max=max" "$OUT" && ! grep -Eq "$RESTARTS" "$MG_CALLS"'
mg_reset inactive
mg_run bash "$SSD" memory-guard --remove
okc "$RC" 0 "memory-guard --remove on an inactive unit exits 0"
ok "memory-guard --remove on an inactive unit sets no runtime property" '! grep -q "set-property" "$MG_CALLS"'
mkdir -p "$UG.d"
printf '[Service]\nMemoryMax=4G\n' > "$GG"
mg_reset active
mg_run bash "$SSD" memory-guard --remove
okc "$RC" 1 "memory-guard --remove refuses a foreign file"
ok "memory-guard --remove leaves a foreign file and runtime alone" \
   'grep -Fxq "MemoryMax=4G" "$GG" && [ ! -s "$MG_CALLS" ]'
rm -f "$GG"
run bash "$SSD" reconcile --remove
okc "$RC" 1 "--remove is refused outside memory-guard"

# Doctor (#2545): a bridge unit without the drop-in, and an active unit whose
# cgroup is still at max, both warn. Read-only: same stub, fake cgroup.
doctor_guard_rows() {
    env CCC_DOCTOR_BRIDGE_UNIT_DIRS="$SDG" CCC_DOCTOR_SYSTEMCTL_BIN="$MG_STUB" \
        CCC_DOCTOR_CGROUP_ROOT="$MG/cg" python3 -c '
import sys
sys.path.insert(0, sys.argv[1] + "/scripts")
from pathlib import Path
from ccc_doctor import Doctor
d = Doctor(repo=Path(sys.argv[1]), claude_dir=Path(sys.argv[2]), scope="all")
d.check_bridge_memory_guard()
for r in d.rows:
    print(f"{r.klass}|{r.item}|{r.status}")
' "$REPO" "$FH/.claude"
}
mg_reset active
# shellcheck disable=SC2034  # DOC_ROWS is read via eval inside ok()
DOC_ROWS="$(doctor_guard_rows)"
ok "doctor warns on a bridge unit without the memory guard drop-in" \
   'grep -q "^경고|bridge memory guard (ccc-telegram-bridge.service)|no memory guard drop-in" <<<"$DOC_ROWS"'
ok "doctor runs only systemctl show (read-only)" '! grep -qv "^show " "$MG_CALLS"'
mg_run bash "$SSD" memory-guard
mg_reset active
# shellcheck disable=SC2034  # DOC_ROWS is read via eval inside ok()
DOC_ROWS="$(doctor_guard_rows)"
ok "doctor warns when the drop-in exists but the active cgroup is still max" \
   'grep -q "^경고|.*memory.max=max.*not applied" <<<"$DOC_ROWS"'
ok "doctor stays read-only on the warning path" '[ -s "$MG_CALLS" ] && ! grep -qv "^show " "$MG_CALLS"'
mg_run bash "$SSD" memory-guard
# shellcheck disable=SC2034  # DOC_ROWS is read via eval inside ok()
DOC_ROWS="$(doctor_guard_rows)"
ok "doctor reports the applied guard as 정상 once memory-guard ran" \
   'grep -q "^정상|.*guard applied to running cgroup (memory.high=1953M memory.max=2929M)" <<<"$DOC_ROWS"'
: > "$SC_CALLS"
run env HOME="$FH" CCC_SYSTEMD_DIR="$SDG" CCC_SYSTEMCTL="$SC_STUB" bash "$SSD" reconcile
ok "reconcile leaves the memory-guard drop-in alone" \
   'grep -Fxq "MemoryMax=75%" "$GG" && grep -q "already canonical" "$OUT"'
printf '[Service]\nMemoryMax=4G\n' > "$GG"
run env HOME="$FH" CCC_SYSTEMD_DIR="$SDG" CCC_SYSTEMCTL="$SC_STUB" bash "$SSD" memory-guard
okc "$RC" 1 "memory-guard refuses a foreign file at its path"
ok "foreign drop-in is left byte-for-byte" '[ "$(cat "$GG")" = "$(printf "[Service]\nMemoryMax=4G")" ]'

# The Matrix frontend unit is hand-installed; memory-guard serves it by name.
SDM="$TMP/sd-matrix"
mkdir -p "$SDM"
printf '[Service]\nExecStart=/bin/true\n' > "$SDM/ccc-matrix-bridge.service"
run env HOME="$FH" CCC_SYSTEMD_DIR="$SDM" CCC_SYSTEMCTL="$SC_STUB" BRIDGE_SERVICE_NAME=ccc-matrix-bridge \
    bash "$SSD" memory-guard
okc "$RC" 0 "memory-guard serves the Matrix unit"
ok "Matrix unit gets the same drop-in" \
   'grep -Fxq "OOMPolicy=continue" "$SDM/ccc-matrix-bridge.service.d/10-ccc-memory-guard.conf"'
run env HOME="$FH" CCC_SYSTEMD_DIR="$TMP/sd-none" CCC_SYSTEMCTL="$SC_STUB" bash "$SSD" memory-guard
okc "$RC" 0 "memory-guard without an installed unit exits 0"
ok "memory-guard without a unit writes nothing" '[ ! -e "$TMP/sd-none" ] && grep -q "not installed" "$OUT"'

# No systemd (Termux): the subcommand is a clean no-op.
run env HOME="$FH" CCC_SYSTEMCTL="$TMP/no-such-systemctl" bash "$SSD" memory-guard
okc "$RC" 0 "memory-guard on a host without systemctl exits 0"
ok "memory-guard reports systemd absent" 'grep -q "no systemd on this host" "$OUT"'

# ---- systemd: validation ----------------------------------------------------
run env CCC_SYSTEMD_DIR="$SD" CCC_SYSTEMCTL="$SC_STUB" bash "$SSD" install
okc "$RC" 1 "install without --project-root exits 1"
run bash "$SSD" --not-a-flag
okc "$RC" 1 "unknown option exits 1"

# ---- launchd: plist generation content -------------------------------------
LPROJ="$TMP/lproj"
mkdir -p "$LPROJ/.telegram_bot"
echo "$$" > "$LPROJ/.telegram_bot/bot.pid"   # live pid => install wait loop returns fast
PLIST="$FH/Library/LaunchAgents/com.telegram-skill-bot.lproj.plist"
run env HOME="$FH" CCC_LAUNCHCTL="$LC_STUB" \
    bash "$SLD" install --project-root "$LPROJ"
okc "$RC" 0 "launchd install exits 0"
ok "plist written under HOME/Library/LaunchAgents" '[ -f "$PLIST" ]'
ok "plist Label matches project slug" \
   'grep -Fq "<string>com.telegram-skill-bot.lproj</string>" "$PLIST"'
ok "plist ProgramArguments runs start.sh"   'grep -Fq "<string>$HERE/start.sh</string>" "$PLIST"'
ok "plist ProgramArguments carries project path" 'grep -Fq "<string>$LPROJ</string>" "$PLIST"'
ok "plist runs the launchd child mode"      'grep -Fq "<string>--_launchd_child</string>" "$PLIST"'
ok "plist keeps the service alive"          'grep -Fq "<key>KeepAlive</key>" "$PLIST"'
ok "plist stdout log under project logs dir" \
   'grep -Fq "<string>$LPROJ/.telegram_bot/logs/launchd_stdout.log</string>" "$PLIST"'
ok "plist WorkingDirectory is repo root"    'grep -Fq "<string>$REPO</string>" "$PLIST"'
ok "plist has no proxy env when unset"      '! grep -q "http_proxy" "$PLIST"'
ok "launchd install bootstrapped the plist" 'grep -q "bootstrap" "$LC_CALLS"'

# ---- launchd: proxy + idempotency ------------------------------------------
# shellcheck disable=SC2034  # consumed via eval in ok()
lbefore="$(cat "$PLIST")"
run env HOME="$FH" CCC_LAUNCHCTL="$LC_STUB" \
    bash "$SLD" install --project-root "$LPROJ"
okc "$RC" 0 "launchd re-install exits 0"
ok "re-install keeps identical plist content" '[ "$lbefore" = "$(cat "$PLIST")" ]'
run env HOME="$FH" CCC_LAUNCHCTL="$LC_STUB" \
    bash "$SLD" install --project-root "$LPROJ" --proxy-url "http://127.0.0.1:3128"
ok "proxy plist carries https_proxy" 'grep -Fq "<string>http://127.0.0.1:3128</string>" "$PLIST" && grep -Fq "<key>https_proxy</key>" "$PLIST"'

# ---- launchd: uninstall (idempotent, clears stale lock) --------------------
echo "99999999" > "$LPROJ/.telegram_bot/bot.pid"   # dead pid: nothing to kill
LOCK="$TMP/token-lock.pid"
echo "99999999" > "$LOCK"                          # stale lock owner => safe to clear
: > "$LC_CALLS"
run env HOME="$FH" CCC_LAUNCHCTL="$LC_STUB" CCC_BRIDGE_TOKEN_LOCK_FILE="$LOCK" \
    bash "$SLD" uninstall --project-root "$LPROJ"
okc "$RC" 0 "launchd uninstall exits 0"
ok "uninstall removed the plist" '[ ! -f "$PLIST" ]'
ok "uninstall booted the service out" 'grep -q "bootout" "$LC_CALLS"'
ok "uninstall removed the stale pid file" '[ ! -f "$LPROJ/.telegram_bot/bot.pid" ]'
ok "uninstall clears the stale PID while preserving the token lock file" '[ -f "$LOCK" ] && [ ! -s "$LOCK" ]'
run env HOME="$FH" CCC_LAUNCHCTL="$LC_STUB" bash "$SLD" uninstall --project-root "$LPROJ"
okc "$RC" 0 "second launchd uninstall exits 0 (idempotent)"
ok "second uninstall reports not installed" 'grep -q "not installed" "$OUT"'

# ---- start.sh dispatch: --uninstall-systemd reaches service-systemd.sh -----
run env HOME="$FH" CCC_SYSTEMD_DIR="$TMP/dispatch-sd" CCC_SYSTEMCTL="$SC_STUB" \
    bash "$START" --path "$PROJECT" --uninstall-systemd
okc "$RC" 0 "start.sh --uninstall-systemd exits 0"
ok "dispatch reached service-systemd.sh" 'grep -q "systemd service not installed" "$OUT"'

# ---- start.sh dispatch: --install pre-flight + plist via subcommand --------
DPROJ="$TMP/dproj"
mkdir -p "$DPROJ/.telegram_bot"
TOKEN="123456:TEST-service-install"
echo "TELEGRAM_BOT_TOKEN=$TOKEN" > "$DPROJ/.telegram_bot/.env"
DH="$TMP/home2"
mkdir -p "$DH"

# Preserved safety: refuse install while an instance is running.
echo "$$" > "$DPROJ/.telegram_bot/bot.pid"
run env HOME="$DH" CCC_LAUNCHCTL="$LC_STUB" bash "$START" --path "$DPROJ" --install
okc "$RC" 1 "start.sh --install refuses while bot is running"
ok "running guard message intact" 'grep -q "already running" "$OUT"'
rm -f "$DPROJ/.telegram_bot/bot.pid"

# Preserved safety: refuse install while the token lock is held by a live pid.
THASH="$(printf '%s' "$TOKEN" | md5sum | cut -d" " -f1)"
mkdir -p "$DH/.telegram-bot-locks"
echo "$$" > "$DH/.telegram-bot-locks/$THASH.pid"
run env HOME="$DH" CCC_LAUNCHCTL="$LC_STUB" bash "$START" --path "$DPROJ" --install
okc "$RC" 1 "start.sh --install refuses while token lock is held"
ok "token-lock guard message intact" 'grep -q "already using the same Bot Token" "$OUT"'
rm -f "$DH/.telegram-bot-locks/$THASH.pid"

# Full dispatch: start.sh pre-flight passes, subcommand writes the plist.
# shellcheck disable=SC2034  # DPLIST is read via eval inside ok()
DPLIST="$DH/Library/LaunchAgents/com.telegram-skill-bot.dproj.plist"
run env HOME="$DH" CCC_LAUNCHCTL="$LC_STUB" bash "$START" --path "$DPROJ" --install
okc "$RC" 0 "start.sh --install dispatch exits 0"
ok "dispatch generated the plist" '[ -f "$DPLIST" ]'
ok "dispatched plist carries project path" 'grep -Fq "<string>$DPROJ</string>" "$DPLIST"'
ok "dispatch hint names start.sh (caller passthrough)" 'grep -q -- "--uninstall to remove startup service" "$OUT" && grep -q "start.sh" "$OUT"'

# ---- systemd: reconcile must not relocate the service (#842) ---------------
# The renderer builds ExecStart from the INVOKING checkout, so `./setup.sh` run
# inside a scratch tree would otherwise repoint the live unit at it. seoseo
# 2026-08-01 served from /work/agent-codebench/ccc-node-pr833 that way.
RSD="$TMP/sd-relocate"
RUNIT="$RSD/ccc-telegram-bridge.service"
mkdir -p "$RSD"
run env HOME="$FH" CCC_SYSTEMD_DIR="$RSD" CCC_SYSTEMCTL="$SC_STUB" \
    bash "$SSD" install --project-root "$PROJECT"
okc "$RC" 0 "relocate fixture installs"

# Point the installed unit at a DIFFERENT checkout, as a work-tree setup run
# would have left it. The path need not exist: a unit whose tree was deleted
# must still be recognized and guarded, not silently taken over.
OTHER="$TMP/other-checkout"
sed -i "s|^ExecStart=/bin/bash $HERE/start\.sh |ExecStart=/bin/bash $OTHER/bridge/start.sh |" "$RUNIT"
sed -i "s|^WorkingDirectory=$REPO\$|WorkingDirectory=$OTHER|" "$RUNIT"
# shellcheck disable=SC2034  # relocate_before is read via eval inside ok()
relocate_before="$(sha256sum < "$RUNIT")"
# shellcheck disable=SC2034  # relocate_inode_before is read via eval inside ok()
relocate_inode_before="$(stat -c %i "$RUNIT")"
: > "$SC_CALLS"
run env HOME="$FH" CCC_SYSTEMD_DIR="$RSD" CCC_SYSTEMCTL="$SC_STUB" \
    bash "$SSD" reconcile
okc "$RC" 0 "reconcile from a foreign checkout exits 0 (setup must not abort)"
ok "foreign-checkout reconcile leaves the unit byte-identical" \
   '[ "$(sha256sum < "$RUNIT")" = "$relocate_before" ]'
ok "foreign-checkout reconcile preserves the inode" \
   '[ "$(stat -c %i "$RUNIT")" = "$relocate_inode_before" ]'
ok "foreign-checkout reconcile does not contact systemctl" '[ ! -s "$SC_CALLS" ]'
ok "refusal names both checkouts" \
   'grep -q "boots from a different checkout" "$OUT" && grep -Fq "$OTHER" "$OUT" && grep -Fq "$REPO" "$OUT"'
ok "refusal points at the explicit escape hatch" 'grep -q -- "--allow-relocate" "$OUT"'
ok "refusal is not mistaken for an unrecognized unit" \
   '! grep -q "not a recognized ccc-generated main unit" "$OUT"'

# Deliberate relocation still works when it says so.
: > "$SC_CALLS"
run env HOME="$FH" CCC_SYSTEMD_DIR="$RSD" CCC_SYSTEMCTL="$SC_STUB" \
    bash "$SSD" reconcile --allow-relocate
okc "$RC" 0 "explicit relocation exits 0"
ok "explicit relocation repoints ExecStart at this checkout" \
   'grep -Fxq "ExecStart=/bin/bash $HERE/start.sh --path $PROJECT" "$RUNIT"'
ok "explicit relocation repoints WorkingDirectory too" \
   'grep -Fxq "WorkingDirectory=$REPO" "$RUNIT"'
ok "explicit relocation still only daemon-reloads" \
   '[ "$(cat "$SC_CALLS")" = "$DAEMON_RELOAD" ]'

# Reconciling in place is untouched by the guard: same checkout, real drift.
sed -i 's/^Restart=always$/Restart=on-failure/' "$RUNIT"
: > "$SC_CALLS"
run env HOME="$FH" CCC_SYSTEMD_DIR="$RSD" CCC_SYSTEMCTL="$SC_STUB" \
    bash "$SSD" reconcile
okc "$RC" 0 "same-checkout drift still reconciles"
ok "same-checkout drift is repaired" \
   'grep -Fxq "Restart=always" "$RUNIT" && ! grep -q "Restart=on-failure" "$RUNIT"'

# The flag is reconcile-only, like --dry-run.
run env HOME="$FH" CCC_SYSTEMD_DIR="$RSD" CCC_SYSTEMCTL="$SC_STUB" \
    bash "$SSD" install --project-root "$PROJECT" --allow-relocate
okc "$RC" 1 "--allow-relocate is rejected outside reconcile"
ok "reconcile-only rejection is explained" 'grep -q "only with reconcile" "$OUT"'

# ---- systemd: ephemeral-HOME refusal (#885) --------------------------------
# Without the CCC_SYSTEMD_DIR seam, a HOME under the tmp tree must never be
# rendered into the unit: a leaked test-suite HOME rewrote a live node's unit
# with HOME=$TMP/wk-home. The guard fires before any path or systemctl touch.
EPH_HOME="$TMP/eph-home"; mkdir -p "$EPH_HOME"
: > "$SC_CALLS"
run env -u CCC_SYSTEMD_DIR -u CCC_SYSTEMD_SCOPE HOME="$EPH_HOME" \
    CCC_SYSTEMCTL="$SC_STUB" bash "$SSD" reconcile --dry-run
okc "$RC" 1 "reconcile refuses an ephemeral tmp HOME without the test seam"
ok "ephemeral-HOME refusal names the incident guard" 'grep -q "#885" "$OUT"'
ok "ephemeral-HOME refusal precedes any systemctl contact" '[ ! -s "$SC_CALLS" ]'

run env -u CCC_SYSTEMD_DIR -u CCC_SYSTEMD_SCOPE HOME="$EPH_HOME" \
    CCC_SYSTEMCTL="$SC_STUB" bash "$SSD" install --project-root "$PROJECT"
okc "$RC" 1 "install refuses an ephemeral tmp HOME without the test seam"

run env -u CCC_SYSTEMD_DIR -u CCC_SYSTEMD_SCOPE HOME="/nonexistent-ccc-885" \
    CCC_SYSTEMCTL="$SC_STUB" bash "$SSD" reconcile --dry-run
okc "$RC" 1 "reconcile refuses a missing HOME directory"
ok "missing-HOME refusal is explained" 'grep -q "does not exist" "$OUT"'

SEAM_FRESH="$TMP/sd-eph-seam"; mkdir -p "$SEAM_FRESH"
run env HOME="$EPH_HOME" CCC_SYSTEMD_DIR="$SEAM_FRESH" CCC_SYSTEMCTL="$SC_STUB" \
    bash "$SSD" reconcile --dry-run
okc "$RC" 0 "the CCC_SYSTEMD_DIR seam still accepts a scratch HOME"
ok "seam-scoped run reports the seam path, not the live tree" \
   'grep -q "reconciliation skipped: $SEAM_FRESH/" "$OUT"'

# --- #1161: callers must name the interpreter for the service scripts --------
# service-{systemd,launchd}.sh declare `#!/bin/bash`, which resolves on neither
# Termux path (no /bin/bash, and no /usr/bin/env either). Exec'ing them there
# dies with 126. setup.sh's `reconcile` call is unguarded, so that 126 aborted
# the whole install and self-update rolled the node back on every run — it sat
# 21 commits behind until this was found.
#
# The scripts already handle a systemd-less host correctly ("systemd unit not
# installed; reconciliation skipped"). The call form was the only thing keeping
# them from reaching that answer.
#
# A text guard on purpose: every functional case in this file drives them as
# `bash "$SSD" ...` — precisely the form the callers lacked — so no functional
# test here can observe a caller exec'ing on the shebang.
# shellcheck disable=SC2034  # uninterpreted is read via eval inside ok()
uninterpreted="$(grep -nE '(^|[[:space:]]|\()(exec[[:space:]]+)?"\$\{?(SCRIPT_DIR|SRC)\}?(/bridge)?/service-(systemd|launchd)\.sh"' \
  "$START" "$REPO/setup.sh" 2>/dev/null | grep -vE '(^|[[:space:]]|\()bash[[:space:]]+"' || true)"
ok "no caller execs the service scripts on their shebang" '[ -z "$uninterpreted" ]'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
