#!/usr/bin/env bash
# install-hande-bridge-termux.sh — stand up the 「한데」 frontend (CCC_CHANNEL=hande)
# on a Termux node (ccc-node #2526; the systemd counterpart is
# install-hande-bridge.sh). Termux has no systemd and no root: the Matrix frontend
# is a runit service ($PREFIX/var/service/ccc-matrix-bridge) whose `run` execs
# ~/.local/share/ccc-matrix/run-matrix, a launcher that resolves the serving
# generation through ~/.ccc-node/bridge-current and execs the bridge with an
# explicit `env -i` environment. Idempotent: re-running rewrites the same files
# and never duplicates list entries. The native relay bot is not touched.
#
# What it does:
#   1. ~/.local/share/ccc-hande/run-hande = run-matrix CLONED with the channel
#      substituted: CCC_CHANNEL=hande, BOT_DATA_DIR/LOGS_DIR/SESSION_STORE_PATH
#      under ~/.ccc-hande, CCC_HANDE_CONFIG_PATH + CCC_HANDE_SOCKET_PATH instead of
#      CCC_MATRIX_CONFIG_PATH, every other CCC_MATRIX_* token dropped,
#      CCC_PUSH_ENABLED=false, CCC_PUSH_SPOOL dropped, webhook nudge off, and the
#      push / fleet-alert relay env sourcing removed (those stay on the Matrix
#      bridge). The relay variables the exec line still names are defined empty so
#      `set -u` cannot trip (the 2026-10-26 daegyo restart loop).
#   2. ~/.config/ccc-hande/config.json (0600): account @<node>:hande, owner, rooms,
#      family users, socket path, socket_mode 0600 (bot and bridge share one uid —
#      no socket_group), approval/tool timeouts, steward on.
#   3. runit service $SVDIR/ccc-hande-bridge/{run,finish,log/run} (+ `down` with
#      --no-start) and the socket dir $PREFIX/var/run/ccc-hande (0700).
#   4. ~/.claude/frontends += hande (the restart-cmd termux-restart-frontends.sh
#      follows bridge-current for ccc-hande-bridge once the service dir exists).
#   5. Readiness report (sv status, socket, health.json).
#
# Usage: install-hande-bridge-termux.sh --node <name> [--room <room>] [--owner-actor owner]
#          [--family-user @actor:hande]... [--approval-timeout 600] [--tool-timeout 90]
#          [--no-start] [--dry-run]
# Env:   PREFIX ($PREFIX, Termux), SVDIR ($PREFIX/var/service), HOME.
# Test seams (never set in production): CCC_HANDE_TERMUX_SV (sv binary),
# CCC_HANDE_SETTLE_SECONDS, CCC_HANDE_CLAUDE_DIR.
set -euo pipefail

NODE="" ROOM="" OWNER_ACTOR="owner" APPROVAL_TIMEOUT=600 TOOL_TIMEOUT=90 NO_START=0 DRY_RUN=0
FAMILY_USERS=()

usage() { sed -n '2,36p' "$0" | sed 's/^# \{0,1\}//'; }
die() { echo "install-hande-bridge-termux: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --node) NODE="${2:-}"; shift 2 ;;
    --room) ROOM="${2:-}"; shift 2 ;;
    --owner-actor) OWNER_ACTOR="${2:-}"; shift 2 ;;
    --family-user) FAMILY_USERS+=("${2:-}"); shift 2 ;;
    --approval-timeout) APPROVAL_TIMEOUT="${2:-}"; shift 2 ;;
    --tool-timeout) TOOL_TIMEOUT="${2:-}"; shift 2 ;;
    --no-start) NO_START=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

H="${HOME:?HOME must be set}"
P="${PREFIX:-/data/data/com.termux/files/usr}"
SVDIR_="${SVDIR:-$P/var/service}"
SV="${CCC_HANDE_TERMUX_SV:-sv}"
CLAUDE_DIR="${CCC_HANDE_CLAUDE_DIR:-$H/.claude}"
SETTLE="${CCC_HANDE_SETTLE_SECONDS:-6}"

SRC="$H/.local/share/ccc-matrix/run-matrix"
DST_DIR="$H/.local/share/ccc-hande"
DST="$DST_DIR/run-hande"
CFG_DIR="$H/.config/ccc-hande"
CFG="$CFG_DIR/config.json"
DATA="$H/.ccc-hande"
RUN_DIR="$P/var/run/ccc-hande"
SOCK="$RUN_DIR/responder.sock"
SVC="ccc-hande-bridge"
SVC_DIR="$SVDIR_/$SVC"
POINTER="$H/.ccc-node/bridge-current"

# ---- preflight -----------------------------------------------------------------
[[ "$NODE" =~ ^[a-z0-9_]{1,32}$ ]] || die "--node must match [a-z0-9_]{1,32} (the actor name, e.g. node1)"
ROOM="${ROOM:-$NODE}"
[[ "$ROOM" =~ ^[A-Za-z0-9_-]{1,64}$ ]] || die "--room must match [A-Za-z0-9_-]{1,64}"
[[ "$OWNER_ACTOR" =~ ^[A-Za-z0-9_]{1,32}$ ]] || die "--owner-actor must match [A-Za-z0-9_]{1,32}"
for u in "${FAMILY_USERS[@]:-}"; do
  [ -z "$u" ] || [[ "$u" =~ ^@[A-Za-z0-9_]{1,32}:hande$ ]] || die "--family-user must look like @actor:hande (got '$u')"
done
[[ "$APPROVAL_TIMEOUT" =~ ^[0-9]+$ && "$TOOL_TIMEOUT" =~ ^[0-9]+$ ]] || die "timeouts must be integers (seconds)"
[ -f "$SRC" ] || die "no $SRC to clone — this installer targets Termux nodes whose Matrix frontend is the runit service launched by run-matrix"
[ -e "$SVDIR_/ccc-matrix-bridge" ] || die "no $SVDIR_/ccc-matrix-bridge runit service — the Matrix frontend must be installed first"
gen="$(readlink -f "$POINTER" 2>/dev/null || true)"
[ -n "$gen" ] && [ -d "$gen" ] || die "$POINTER does not resolve — prepare a generation first (termux-prepare-generation.sh)"
[ -d "$gen/source/bridge/core/hande" ] || die "serving generation $(basename "$gen") has no bridge/core/hande — self-update / prepare a newer generation first (hande frontend not deployed here)"
grep -q 'CCC_CHANNEL=matrix' "$SRC" || die "$SRC is not a Matrix launcher (no CCC_CHANNEL=matrix)"
grep -q 'CCC_MATRIX_CONFIG_PATH=' "$SRC" || die "$SRC has no CCC_MATRIX_CONFIG_PATH token to substitute"

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

say "== install-hande-bridge-termux: node=$NODE room=$ROOM generation=$(basename "$gen") svdir=$SVDIR_ data=$DATA"

# ---- 1. run-hande = run-matrix with the channel substituted ---------------------------
# python3 is part of the Termux bridge prerequisites. Line-based: sourcing lines
# are dropped by name, the exec line is rewritten token by token, and the result
# is refused if any Matrix-only token survives.
run_hande="$(python3 - "$SRC" "$H" "$CFG" "$SOCK" <<'PY'
import re, sys
src, home, cfg, sock = sys.argv[1:5]
out = []
inserted_defs = False
DEFS = ('CCC_PUSH_FLEET_RELAY_URL=""; CCC_PUSH_FLEET_RELAY_SECRET_FILE=""; '
        'CCC_PUSH_FLEET_NODE=""; CCC_PUSH_FLEET_RELAY_DIRECT_EVENTS=""  '
        '# relay stays on the Matrix bridge; defined empty so set -u holds (ccc-node #2526)')
for i, line in enumerate(open(src, encoding="utf-8").read().split("\n")):
    s = line.strip()
    if i == 0:
        out.append(line)
        out.append("# hande frontend cloned from run-matrix by ccc-node scripts/install-hande-bridge-termux.sh (#2526).")
        out.append("# Regenerate by re-running the installer; edits here are overwritten.")
        continue
    # push / fleet-alert relay / webhook nudge sourcing: Matrix-only
    if re.search(r'\b(NUDGE_ENV|RELAY_ENV)\b', s) or re.match(r'#.*(webhook nudge|fleet alert relay)', s):
        continue
    if re.match(r'^(CCC_PUSH_FLEET_[A-Z_]+="";?\s*)+$', s):
        continue  # the old hand-port's empty defs; replaced by DEFS below
    if s.startswith("exec ") and "CCC_CHANNEL=matrix" in s:
        if not inserted_defs:
            out.append(DEFS); inserted_defs = True
        for a, b in ((f"{home}/.ccc-matrix", f"{home}/.ccc-hande"),
                     ("CCC_CHANNEL=matrix", "CCC_CHANNEL=hande"),
                     ("CCC_PUSH_ENABLED=true", "CCC_PUSH_ENABLED=false")):
            if a not in line:
                sys.exit(f"pattern not found in exec line: {a}")
            line = line.replace(a, b)
        line = re.sub(r'CCC_MATRIX_CONFIG_PATH=("[^"]*"|\S+)',
                      f"CCC_HANDE_CONFIG_PATH={cfg} CCC_HANDE_SOCKET_PATH={sock}", line, count=1)
        line = re.sub(r'\s*CCC_MATRIX_INITIALIZE=("[^"]*"|\S+)', "", line)
        line = re.sub(r'\s*CCC_PUSH_SPOOL=("[^"]*"|\S+)', "", line)
        line = re.sub(r'CCC_WEBHOOK_NUDGE_ENABLED=("[^"]*"|\S+)', "CCC_WEBHOOK_NUDGE_ENABLED=false", line)
        line = re.sub(r'\s*CCC_MATRIX_[A-Z_]+=("[^"]*"|\S+)', "", line)  # every other Matrix-only token
    out.append(line)
text = "\n".join(out)
if not inserted_defs:
    sys.exit("no exec line with CCC_CHANNEL=matrix found")
for must in ("CCC_CHANNEL=hande", "CCC_HANDE_CONFIG_PATH=", "CCC_HANDE_SOCKET_PATH=", "CCC_PUSH_ENABLED=false", f"{home}/.ccc-hande"):
    if must not in text:
        sys.exit(f"transform failed: missing {must}")
for bad in ("CCC_CHANNEL=matrix", "CCC_MATRIX_", f"{home}/.ccc-matrix", "CCC_PUSH_ENABLED=true", "CCC_PUSH_SPOOL=", "NUDGE_ENV", "RELAY_ENV"):
    if bad in text:
        sys.exit(f"transform left a Matrix-only token: {bad}")
sys.stdout.write(text)
PY
)" || die "run-matrix transform failed"
write_file "$DST" 0700 <<<"$run_hande"

# ---- 2. config.json ---------------------------------------------------------------
family_json="[]"
if [ "${#FAMILY_USERS[@]}" -gt 0 ] && [ -n "${FAMILY_USERS[0]}" ]; then
  family_json="$(printf '"%s",' "${FAMILY_USERS[@]}")"; family_json="[${family_json%,}]"
fi
[ "$DRY_RUN" = 1 ] || { mkdir -p "$CFG_DIR" "$DATA/logs" "$RUN_DIR"; chmod 0700 "$CFG_DIR" "$DATA" "$RUN_DIR"; }
write_file "$CFG" 0600 <<EOF
{
  "account": "@$NODE:hande",
  "owner": "@$OWNER_ACTOR:hande",
  "rooms": ["$ROOM"],
  "family_rooms": [],
  "family_users": $family_json,
  "socket_path": "$SOCK",
  "socket_mode": "0600",
  "approval_timeout_s": $APPROVAL_TIMEOUT,
  "tool_timeout_s": $TOOL_TIMEOUT,
  "interim_frames": false,
  "notice_frames": false,
  "steward_tools": true,
  "steward_tools_approve_all": false
}
EOF
[ "$DRY_RUN" = 1 ] || python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$CFG"

# ---- 3. runit service ------------------------------------------------------------------
# runsvdir picks a new service directory up on its next scan (~5 s). `down` keeps it
# from auto-starting (--no-start); the normal path removes a stale `down`.
write_file "$SVC_DIR/finish" 0700 <<EOF
#!$P/bin/sh
sleep 5
EOF
write_file "$SVC_DIR/log/run" 0700 <<EOF
#!$P/bin/sh
mkdir -p $DATA/logs
exec svlogd -tt $DATA/logs
EOF
write_file "$SVC_DIR/run" 0700 <<EOF
#!$P/bin/sh
exec 2>&1
exec $DST
EOF
if [ "$NO_START" = 1 ]; then
  write_file "$SVC_DIR/down" 0600 <<<"# ccc-node #2526: installed with --no-start; remove this file (or sv up $SVC) to start"
elif [ "$DRY_RUN" = 0 ] && [ -e "$SVC_DIR/down" ]; then
  rm -f "$SVC_DIR/down"; say "removed $SVC_DIR/down"
fi

# ---- 4. operator list ----------------------------------------------------------------------
append_once "$CLAUDE_DIR/frontends" "hande" "# $(date +%F) hande frontend (ccc-node #2526, install-hande-bridge-termux.sh)"

# ---- 5. start + report -----------------------------------------------------------------------
if [ "$DRY_RUN" = 1 ]; then say "dry-run: nothing written, nothing started"; exit 0; fi
if [ "$NO_START" = 1 ]; then say "--no-start: service dir written with 'down'; start later with: rm $SVC_DIR/down && SVDIR=$SVDIR_ $SV up $SVC"; exit 0; fi
SVDIR="$SVDIR_" "$SV" up "$SVC" >/dev/null 2>&1 || say "sv up $SVC: not supervised yet (runsvdir scans every ~5 s)"
sleep "$SETTLE"
say "sv: $(SVDIR="$SVDIR_" "$SV" status "$SVC" 2>&1 | head -n 1)"
if [ -S "$SOCK" ]; then say "socket: $(stat -c '%A %U %n' "$SOCK")"; else say "socket: not yet at $SOCK (bridge still starting?)"; fi
if [ -f "$DATA/health.json" ]; then
  python3 - "$DATA/health.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
print("health:", (d.get("service") or {}).get("state"), "| generation:", ((d.get("runtime_generation") or {}).get("python_prefix") or "?").split("/preparations/")[-1].split("/")[0])
PY
else
  say "health: $DATA/health.json not written yet"
fi
say "next: point the node's native relay bot at $SOCK (bot-side responder setting, separate approval); self-update restarts follow bridge-current via termux-restart-frontends.sh"
say "rollback: SVDIR=$SVDIR_ $SV down $SVC; rm -rf $SVC_DIR $DST_DIR $CFG_DIR $RUN_DIR; drop the appended frontends line (backup *.bak-hande-*)"
