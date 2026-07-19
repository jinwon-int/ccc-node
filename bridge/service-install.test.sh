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
if [ "$(id -u)" = "0" ]; then WANTED="multi-user.target"; else WANTED="default.target"; fi

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
