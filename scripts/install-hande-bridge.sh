#!/usr/bin/env bash
# install-hande-bridge.sh — stand up the 「한데」 frontend (CCC_CHANNEL=hande) on a
# node whose Matrix frontend runs as a *system* unit (ccc-node #2380, rollout
# step 1–4 of docs/hande-frontend.md). Root. Idempotent: re-running rewrites the
# same files and never duplicates list entries.
#
# What it does (nothing touches the native relay bot — that is the bot installer
# in family-messenger and the drop-in switch, step 5, which stays a separate
# approval):
#   1. /etc/tmpfiles.d/ccc-hande.conf  → /run/ccc-hande 2770 <run-user>:<bot-group>
#      (setgid so the socket the bridge creates is reachable by the bot's group)
#   2. /etc/ccc-hande/config.json (0600): account @<node>:hande, owner, rooms,
#      family users, socket path/mode/group, approval/tool timeouts, steward on
#   3. ccc-hande-bridge.service = the node's Matrix unit CLONED with the channel
#      substituted (same User/HOME/PROJECT_ROOT/ExecStart, BOT_DATA_DIR → ~/.ccc-hande,
#      CCC_HANDE_CONFIG_PATH instead of CCC_MATRIX_CONFIG_PATH), plus its drop-ins
#      EXCEPT push / fleet-alert relay / webhook nudge, plus a last drop-in pinning
#      CCC_PUSH_ENABLED=false. So the hande bridge gets exactly the provider,
#      model, effort, execution profile and allowlist the Matrix bridge has.
#   4. ~/.claude/frontends += hande; ~/.claude/self-update.services += unit
#   5. daemon-reload, enable --now, then a short readiness report
#
# Usage: install-hande-bridge.sh --node <name> [--room <room>] [--owner-actor owner]
#          [--family-user @actor:hande]... [--clone-from ccc-matrix-bridge.service]
#          [--bot-group native-relay-bot] [--approval-timeout 600] [--tool-timeout 90]
#          [--no-start] [--dry-run]
# The checkout is read off the cloned unit (WorkingDirectory=<checkout>/bridge, else
# the ExecStart interpreter path), so /root/ccc-node nodes work unaided (#2526).
# Termux (runit, no root) nodes use install-hande-bridge-termux.sh instead.
# Test seams (never set in production): CCC_HANDE_INSTALL_ROOT (path prefix for
# /etc and /run), CCC_HANDE_SYSTEMCTL, CCC_HANDE_TMPFILES, CCC_HANDE_CCC_NODE
# (overrides the detected checkout), CCC_HANDE_CLAUDE_DIR, CCC_HANDE_SETTLE_SECONDS.
set -euo pipefail

NODE="" ROOM="" OWNER_ACTOR="owner" CLONE="ccc-matrix-bridge.service" BOT_GROUP="native-relay-bot"
APPROVAL_TIMEOUT=600 TOOL_TIMEOUT=90 NO_START=0 DRY_RUN=0
FAMILY_USERS=()

usage() { sed -n '2,32p' "$0" | sed 's/^# \{0,1\}//'; }
die() { echo "install-hande-bridge: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --node) NODE="${2:-}"; shift 2 ;;
    --room) ROOM="${2:-}"; shift 2 ;;
    --owner-actor) OWNER_ACTOR="${2:-}"; shift 2 ;;
    --family-user) FAMILY_USERS+=("${2:-}"); shift 2 ;;
    --clone-from) CLONE="${2:-}"; shift 2 ;;
    --bot-group) BOT_GROUP="${2:-}"; shift 2 ;;
    --approval-timeout) APPROVAL_TIMEOUT="${2:-}"; shift 2 ;;
    --tool-timeout) TOOL_TIMEOUT="${2:-}"; shift 2 ;;
    --no-start) NO_START=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

ROOT="${CCC_HANDE_INSTALL_ROOT:-}"
SYSTEMCTL="${CCC_HANDE_SYSTEMCTL:-systemctl}"
TMPFILES="${CCC_HANDE_TMPFILES:-systemd-tmpfiles}"
CLAUDE_DIR="${CCC_HANDE_CLAUDE_DIR:-${HOME:-/root}/.claude}"
SETTLE="${CCC_HANDE_SETTLE_SECONDS:-6}"

UNIT_DIR="$ROOT/etc/systemd/system"
UNIT_NAME="ccc-hande-bridge.service"
CONF_DIR_REAL="/etc/ccc-hande"
CONF_DIR="$ROOT$CONF_DIR_REAL"
RUN_DIR_REAL="/run/ccc-hande"
SOCKET_REAL="$RUN_DIR_REAL/responder.sock"
TMPFILES_CONF="$ROOT/etc/tmpfiles.d/ccc-hande.conf"

# ---- preflight -----------------------------------------------------------------
[[ "$NODE" =~ ^[a-z0-9_]{1,32}$ ]] || die "--node must match [a-z0-9_]{1,32} (the actor name, e.g. node1)"
ROOM="${ROOM:-$NODE}"
[[ "$ROOM" =~ ^[A-Za-z0-9_-]{1,64}$ ]] || die "--room must match [A-Za-z0-9_-]{1,64}"
[[ "$OWNER_ACTOR" =~ ^[A-Za-z0-9_]{1,32}$ ]] || die "--owner-actor must match [A-Za-z0-9_]{1,32}"
for u in "${FAMILY_USERS[@]:-}"; do
  [ -z "$u" ] || [[ "$u" =~ ^@[A-Za-z0-9_]{1,32}:hande$ ]] || die "--family-user must look like @actor:hande (got '$u')"
done
[[ "$APPROVAL_TIMEOUT" =~ ^[0-9]+$ && "$TOOL_TIMEOUT" =~ ^[0-9]+$ ]] || die "timeouts must be integers (seconds)"
getent group "$BOT_GROUP" >/dev/null 2>&1 || die "group '$BOT_GROUP' does not exist — install the native relay bot first (it owns that group)"
CLONE_PATH="$UNIT_DIR/$CLONE"
[ -f "$CLONE_PATH" ] || die "no $CLONE_PATH to clone — this installer targets nodes whose Matrix frontend is a system unit"

# ccc-node #2526: the checkout is wherever the Matrix unit runs from, not a fixed
# /opt/ccc-node — two fleet nodes keep it at /root/ccc-node. Read it off the unit we
# clone (WorkingDirectory=<checkout>/bridge, else the ExecStart interpreter path
# <checkout>/bridge/venv/bin/python); the old default stays as the last resort.
# The hande code is checked in THAT checkout, so a /root/ccc-node node no longer
# needs the CCC_HANDE_CCC_NODE test seam to get past preflight.
detect_checkout() {
  local wd es
  wd="$(sed -n 's/^WorkingDirectory=\(.*\)$/\1/p' "$CLONE_PATH" | tail -1)"
  case "$wd" in */bridge) printf '%s unit-WorkingDirectory\n' "${wd%/bridge}"; return 0 ;; esac
  es="$(sed -n 's|^ExecStart=\([^ ]*\)/bridge/venv/bin/python[0-9.]* .*$|\1|p' "$CLONE_PATH" | tail -1)"
  if [ -n "$es" ]; then printf '%s unit-ExecStart\n' "$es"; return 0; fi
  printf '/opt/ccc-node default\n'
}
if [ -n "${CCC_HANDE_CCC_NODE:-}" ]; then
  CCC_NODE="$CCC_HANDE_CCC_NODE"; checkout_from="CCC_HANDE_CCC_NODE"
else
  read -r CCC_NODE checkout_from < <(detect_checkout)
fi
[ -d "$CCC_NODE/bridge/core/hande" ] || die "$CCC_NODE (from $checkout_from) has no bridge/core/hande — run self-update first (hande frontend not deployed here)"

run_user="$(sed -n 's/^User=\(.*\)$/\1/p' "$CLONE_PATH" | tail -1)"; run_user="${run_user:-root}"
run_home="$(sed -n 's/^Environment=HOME=\(.*\)$/\1/p' "$CLONE_PATH" | tail -1)"; run_home="${run_home:-/root}"
matrix_data="$(sed -n 's/^Environment=BOT_DATA_DIR=\(.*\)$/\1/p' "$CLONE_PATH" | tail -1)"
hande_data="${matrix_data/.ccc-matrix/.ccc-hande}"
[ -n "$matrix_data" ] && [ "$hande_data" != "$matrix_data" ] || hande_data="$run_home/.ccc-hande"
grep -q '^Environment=CCC_CHANNEL=matrix$' "$CLONE_PATH" || die "$CLONE is not a Matrix frontend unit (no CCC_CHANNEL=matrix)"

say() { printf '%s\n' "$*"; }
write_file() { # <path> <mode> ; content on stdin
  local path="$1" mode="$2" tmp
  if [ "$DRY_RUN" = 1 ]; then say "would write $path ($mode)"; cat >/dev/null; return 0; fi
  mkdir -p "$(dirname "$path")"
  tmp="$(mktemp "$(dirname "$path")/.tmp.XXXXXX")"
  cat >"$tmp"; chmod "$mode" "$tmp"; mv -f "$tmp" "$path"
  say "wrote $path ($mode)"
}
append_once() { # <file> <line> <comment>
  local file="$1" line="$2" comment="$3"
  if [ -f "$file" ] && grep -qxF "$line" "$file"; then say "ok  $file already has '$line'"; return 0; fi
  if [ "$DRY_RUN" = 1 ]; then say "would append '$line' to $file"; return 0; fi
  mkdir -p "$(dirname "$file")"
  [ -f "$file" ] && cp -a "$file" "$file.bak-hande-$(date +%Y%m%dT%H%M%S)"
  printf '%s\n%s\n' "$comment" "$line" >>"$file"
  say "appended '$line' to $file"
}

say "== install-hande-bridge: node=$NODE room=$ROOM run-user=$run_user bot-group=$BOT_GROUP clone=$CLONE data=$hande_data checkout=$CCC_NODE ($checkout_from)"

# ---- 1. socket directory (tmpfiles) ---------------------------------------------
write_file "$TMPFILES_CONF" 0644 <<EOF
# ccc-node #2380: socket dir for the hande bridge. setgid: the socket the bridge
# creates inherits group $BOT_GROUP so the native relay bot can connect.
d $RUN_DIR_REAL 2770 $run_user $BOT_GROUP -
EOF
if [ "$DRY_RUN" = 0 ]; then "$TMPFILES" --create "$TMPFILES_CONF" >/dev/null; fi

# ---- 2. config.json ---------------------------------------------------------------
family_json="[]"
if [ "${#FAMILY_USERS[@]}" -gt 0 ] && [ -n "${FAMILY_USERS[0]}" ]; then
  family_json="$(printf '"%s",' "${FAMILY_USERS[@]}")"; family_json="[${family_json%,}]"
fi
if [ "$DRY_RUN" = 0 ]; then mkdir -p "$CONF_DIR"; chmod 0700 "$CONF_DIR"; chown "$run_user" "$CONF_DIR" 2>/dev/null || true; fi
write_file "$CONF_DIR/config.json" 0600 <<EOF
{
  "account": "@$NODE:hande",
  "owner": "@$OWNER_ACTOR:hande",
  "rooms": ["$ROOM"],
  "family_rooms": [],
  "family_users": $family_json,
  "socket_path": "$SOCKET_REAL",
  "socket_mode": "0660",
  "socket_group": "$BOT_GROUP",
  "approval_timeout_s": $APPROVAL_TIMEOUT,
  "tool_timeout_s": $TOOL_TIMEOUT,
  "interim_frames": false,
  "notice_frames": false,
  "steward_tools": true,
  "steward_tools_approve_all": false
}
EOF
[ "$DRY_RUN" = 1 ] || { chown "$run_user" "$CONF_DIR/config.json" 2>/dev/null || true; python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$CONF_DIR/config.json"; }

# ---- 3. unit = Matrix unit with the channel substituted ------------------------------
transform_unit() {
  sed -E \
    -e 's|^Description=.*$|Description=ccc-node hande frontend (family messenger native relay, CCC_CHANNEL=hande)|' \
    -e 's|^Environment=CCC_CHANNEL=matrix$|Environment=CCC_CHANNEL=hande|' \
    -e "s|^Environment=BOT_DATA_DIR=.*$|Environment=BOT_DATA_DIR=$hande_data|" \
    -e "s|^Environment=CCC_MATRIX_CONFIG_PATH=.*$|Environment=CCC_HANDE_CONFIG_PATH=$CONF_DIR_REAL/config.json\nEnvironment=CCC_HANDE_SOCKET_PATH=$SOCKET_REAL|" \
    -e '/^Environment=CCC_MATRIX_/d' \
    -e '/^Environment="CCC_MATRIX_/d'
}
unit_body="$(transform_unit <"$CLONE_PATH")"
grep -q '^Environment=CCC_CHANNEL=hande$' <<<"$unit_body" || die "unit transform failed (CCC_CHANNEL)"
grep -q "^Environment=CCC_HANDE_CONFIG_PATH=" <<<"$unit_body" || die "unit transform failed (config path)"
! grep -q 'CCC_MATRIX_' <<<"$unit_body" || die "unit transform left a CCC_MATRIX_* line"
write_file "$UNIT_DIR/$UNIT_NAME" 0644 <<<"$unit_body"

# drop-ins: provider / profile parity, minus the ones that must not run twice
# The copied main-model drop-in (zzzzzzzzzzzzzz-main-*.conf) keeps pointing at the
# MATRIX env file in /root/.config/ccc-main. Model switches must therefore go
# through scripts/main-model-switch.py (#2557), which repoints every unit that
# shares that env file before retiring it -- never a node-local apply.sh (#2571).
dropin_src="$UNIT_DIR/$CLONE.d"
dropin_dst="$UNIT_DIR/$UNIT_NAME.d"
copied=0 skipped=0
if [ -d "$dropin_src" ]; then
  for f in "$dropin_src"/*.conf; do
    [ -f "$f" ] || continue
    base="$(basename "$f")"
    case "$base" in
      *push*|*fleet-alert*|*nudge*|*webhook*) say "skip drop-in $base (push/relay/nudge stay on the Matrix bridge)"; skipped=$((skipped+1)); continue ;;
    esac
    body="$(sed -E -e '/^Environment=CCC_MATRIX_/d' -e '/^Environment="CCC_MATRIX_/d' -e "s|\.ccc-matrix|.ccc-hande|g" "$f")"
    write_file "$dropin_dst/$base" 0644 <<<"$body"
    copied=$((copied+1))
  done
fi
write_file "$dropin_dst/zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz-hande.conf" 0644 <<'EOF'
# ccc-node #2380: the hande bridge never consumes the node's push spool (the Matrix
# bridge does; a second consumer makes records vanish at random) and runs no
# fleet-alert relay or webhook nudge of its own. Last drop-in by name so it wins.
[Service]
Environment=CCC_PUSH_ENABLED=false
UnsetEnvironment=CCC_PUSH_FLEET_RELAY_URL CCC_PUSH_FLEET_RELAY_SECRET CCC_PUSH_FLEET_RELAY_SECRET_FILE CCC_PUSH_FLEET_NODE CCC_PUSH_MIRROR_DIRS CCC_PUSH_CONSUME_SPOOL CCC_WEBHOOK_NUDGE_ENABLED CCC_WEBHOOK_NUDGE_PORT CCC_MATRIX_WEBHOOK_NUDGE_PORT
EOF
say "drop-ins: copied=$copied skipped=$skipped (+ hande pin)"

# ---- 4. operator lists -------------------------------------------------------------
append_once "$CLAUDE_DIR/frontends" "hande" "# $(date +%F) hande frontend (ccc-node #2380, install-hande-bridge.sh)"
append_once "$CLAUDE_DIR/self-update.services" "$UNIT_NAME" "# $(date +%F) hande frontend unit (ccc-node #2380, install-hande-bridge.sh)"

# ---- 5. start + report ---------------------------------------------------------------
if [ "$DRY_RUN" = 1 ]; then say "dry-run: not reloading/starting"; exit 0; fi
"$SYSTEMCTL" daemon-reload
if [ "$NO_START" = 1 ]; then say "--no-start: unit written, not enabled"; exit 0; fi
"$SYSTEMCTL" enable --now "$UNIT_NAME" >/dev/null 2>&1 || "$SYSTEMCTL" enable --now "$UNIT_NAME"
sleep "$SETTLE"
state="$("$SYSTEMCTL" show "$UNIT_NAME" -p ActiveState --value 2>/dev/null || echo unknown)"
say "unit $UNIT_NAME: $state"
if [ -S "$ROOT$SOCKET_REAL" ]; then
  say "socket: $(stat -c '%A %U:%G %n' "$ROOT$SOCKET_REAL")"
else
  say "socket: not yet at $SOCKET_REAL (bridge still starting?)"
fi
if [ -f "$hande_data/health.json" ]; then
  python3 - "$hande_data/health.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
print("health:", d.get("service"), "| transport:", (d.get("telegram") or {}).get("state"))
PY
else
  say "health: $hande_data/health.json not written yet"
fi
say "next: step 5 (separate approval) — point the bot at $SOCKET_REAL via native-relay-bot.service.d/30-agent.conf and restart it"
say "rollback: $SYSTEMCTL disable --now $UNIT_NAME; remove $UNIT_NAME(.d), $TMPFILES_CONF, $CONF_DIR; drop the two appended list lines (backups *.bak-hande-*)"
