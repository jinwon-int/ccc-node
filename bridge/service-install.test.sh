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

# Stub systemctl / launchctl: record every invocation, always succeed.
SC_CALLS="$TMP/systemctl.calls"
SC_STUB="$TMP/systemctl"
printf '#!/usr/bin/env bash\necho "$*" >> "%s"\nexit 0\n' "$SC_CALLS" > "$SC_STUB"
chmod +x "$SC_STUB"
LC_CALLS="$TMP/launchctl.calls"
LC_STUB="$TMP/launchctl"
printf '#!/usr/bin/env bash\necho "$*" >> "%s"\nexit 0\n' "$LC_CALLS" > "$LC_STUB"
chmod +x "$LC_STUB"

PROJECT="$TMP/myproj"
mkdir -p "$PROJECT"
FH="$TMP/home"
mkdir -p "$FH"

# Unit scope follows the euid running the test (root => system scope).
if [ "$(id -u)" = "0" ]; then
    WANTED="multi-user.target"
    DAEMON_RELOAD="daemon-reload"
else
    WANTED="default.target"
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
ok "unit WorkingDirectory is repo root" 'grep -Fxq "WorkingDirectory=$REPO" "$UNIT"'
ok "unit WantedBy matches scope"        'grep -Fxq "WantedBy=$WANTED" "$UNIT"'
ok "unit has no proxy env when unset"   '! grep -q "http_proxy" "$UNIT"'
ok "unit has no blank lines (sed collapse)" '! grep -q "^$" "$UNIT"'
ok "systemd install ran daemon-reload"  'grep -q "daemon-reload" "$SC_CALLS"'
ok "systemd install enabled --now the service" \
   'grep -q "enable --now ccc-telegram-bridge.service" "$SC_CALLS"'

# ---- systemd: setup/self-update reconciliation -----------------------------
# Identical canonical content is a true no-op: no inode replacement, reload,
# enable, or restart.
: > "$SC_CALLS"
unit_inode_before="$(stat -c %i "$UNIT")"
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
cat > "$ROLLBACK_STUB" <<SH
#!/usr/bin/env bash
echo "\$*" >> "$ROLLBACK_CALLS"
count="\$(cat "$ROLLBACK_COUNT" 2>/dev/null || echo 0)"
count=\$((count + 1))
printf '%s' "\$count" > "$ROLLBACK_COUNT"
[ "\$count" = 1 ] && exit 1
exit 0
SH
chmod +x "$ROLLBACK_STUB"
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
cat > "$ALWAYS_FAIL_STUB" <<SH
#!/usr/bin/env bash
echo "\$*" >> "$ALWAYS_FAIL_CALLS"
exit 1
SH
chmod +x "$ALWAYS_FAIL_STUB"
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
symlink_inode_before="$(stat -c %i "$SYMLINK_UNIT")"
symlink_target_before="$(readlink "$SYMLINK_UNIT")"
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
HARDLINK_SD="$TMP/hardlink-sd"
HARDLINK_PEER="$TMP/hardlink-peer.service"
mkdir -p "$HARDLINK_SD"
cp "$UNIT" "$HARDLINK_PEER"
sed -i 's/^Restart=always$/Restart=on-failure/' "$HARDLINK_PEER"
HARDLINK_UNIT="$HARDLINK_SD/ccc-telegram-bridge.service"
ln "$HARDLINK_PEER" "$HARDLINK_UNIT"
hardlink_inode_before="$(stat -c %i "$HARDLINK_UNIT")"
hardlink_count_before="$(stat -c %h "$HARDLINK_UNIT")"
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
run env HOME="$FH" CCC_SYSTEMD_DIR="$SD" CCC_SYSTEMCTL="$SC_STUB" bash "$SSD" uninstall
okc "$RC" 0 "second uninstall exits 0 (idempotent)"
ok "second uninstall reports not installed" 'grep -q "not installed" "$OUT"'

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
ok "uninstall cleared the stale token lock" '[ ! -f "$LOCK" ]'
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
DPLIST="$DH/Library/LaunchAgents/com.telegram-skill-bot.dproj.plist"
run env HOME="$DH" CCC_LAUNCHCTL="$LC_STUB" bash "$START" --path "$DPROJ" --install
okc "$RC" 0 "start.sh --install dispatch exits 0"
ok "dispatch generated the plist" '[ -f "$DPLIST" ]'
ok "dispatched plist carries project path" 'grep -Fq "<string>$DPROJ</string>" "$DPLIST"'
ok "dispatch hint names start.sh (caller passthrough)" 'grep -q -- "--uninstall to remove startup service" "$OUT" && grep -q "start.sh" "$OUT"'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
