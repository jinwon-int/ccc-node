#!/usr/bin/env bash
# shellcheck disable=SC2016 # remote scripts are single-quoted on purpose
# fleet-ssh-mesh.sh — full-mesh SSH provisioning for a fleet of nodes (#2687).
#
# Every node gets its own fleet keypair (~/.ssh/id_ed25519_fleet). This script
# collects the PUBLIC halves, scans each node's ed25519 host key, and writes
# three managed blocks on every node so that any node can reach any other:
#   ~/.ssh/authorized_keys  all fleet public keys            (appended, 600)
#   ~/.ssh/known_hosts      all scanned host keys            (appended, 644)
#   ~/.ssh/config           one "Host <name>" entry per node (prepended, 600)
#
# Phases (default: all = collect scan build push; verify is run separately):
#   collect  per node: create the fleet keypair if missing, print its .pub
#   scan     ssh-keyscan -t ed25519 every node
#   build    render the three blocks into the work dir
#   push     replace the managed block in each file on each node
#            (--dry-run: report per node/file what would change, write nothing)
#   verify   N x N check: every node ssh's to every other node by alias
#
# Node table — kept OUT of this script and out of the repo. Default path:
#   ${CCC_FLEET_SSH_NODES:-$HOME/.config/ccc/fleet-ssh-nodes}   (or --nodes FILE)
# One node per line, whitespace separated; '#' starts a comment:
#   <name> <port> <user> [host]
# Without <host>, the address is resolved at run time from
# `tailscale status --json` (a peer whose HostName or MagicDNS short name equals
# <name>; its first IPv4 tailnet address). Not found or more than one matching
# device fails closed. collect/push/verify reach nodes by their ssh alias
# (<name>), exactly as the managed ~/.ssh/config block defines it.
#
# Safety properties:
#   - Private keys never leave their node: collect only ever reads the .pub.
#   - Before any remote file is rewritten, a timestamped copy is kept next to
#     it (<file>.bak-YYYYmmddHHMMSS[.N], never overwritten).
#   - Lines outside the marker block are preserved; an unbalanced marker block
#     makes push refuse that file instead of guessing.
#   - Idempotent: re-running build + push yields byte-identical files.
#   - A missing/invalid node table, or an unresolvable host, stops the run
#     before any remote action. build refuses a partial collect/scan (which
#     would drop a node's key from every authorized_keys) unless
#     --allow-partial is given.
#
# Test seams: SSH_BIN (or CCC_FLEET_SSH), SSH_KEYSCAN_BIN, TAILSCALE_BIN,
# CCC_FLEET_SSH_WORK (work dir), CCC_FLEET_SSH_MARKER (marker label).
set -u -o pipefail

PROG=fleet-ssh-mesh
SSH_BIN="${SSH_BIN:-${CCC_FLEET_SSH:-ssh}}"
SSH_KEYSCAN_BIN="${SSH_KEYSCAN_BIN:-ssh-keyscan}"
TAILSCALE_BIN="${TAILSCALE_BIN:-tailscale}"
WORK="${CCC_FLEET_SSH_WORK:-$HOME/.claude/state/fleet-mesh}"
NODES_FILE="${CCC_FLEET_SSH_NODES:-$HOME/.config/ccc/fleet-ssh-nodes}"
MARKER="${CCC_FLEET_SSH_MARKER:-SEOYOON-FLEET-MESH}"
STAMP=$(date +%Y%m%d%H%M%S)
KEY_DATE=$(date +%Y%m%d)
DRY_RUN=0
ALLOW_PARTIAL=0
PHASE=""

usage() {
  cat <<'EOF'
Usage: fleet-ssh-mesh.sh [--nodes FILE] [--dry-run] [--allow-partial]
                         {collect|scan|build|push|verify|all}

Node table: --nodes FILE, else $CCC_FLEET_SSH_NODES, else
~/.config/ccc/fleet-ssh-nodes. One "<name> <port> <user> [host]" per line;
without host the tailnet address comes from `tailscale status --json`.
--dry-run       push only: show what would change per node/file, write nothing
--allow-partial build even if fewer keys/host keys than nodes were collected
Exit: 0 ok, 1 a node or step failed, 2 usage/config/resolution error.
EOF
}

die() { echo "$PROG: $*" >&2; exit 2; }

while [ "$#" -gt 0 ]; do
  case "$1" in
    --nodes) [ "$#" -ge 2 ] || die "--nodes needs a FILE"; NODES_FILE="$2"; shift 2 ;;
    --nodes=*) NODES_FILE="${1#--nodes=}"; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --allow-partial) ALLOW_PARTIAL=1; shift ;;
    -h|--help) usage; exit 0 ;;
    collect|scan|build|push|verify|all)
      [ -z "$PHASE" ] || die "only one phase may be given"
      PHASE="$1"; shift ;;
    *) echo "$PROG: unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done
PHASE="${PHASE:-all}"
if [ "$DRY_RUN" = 1 ] && [ "$PHASE" != push ]; then
  die "--dry-run is supported for the push phase only"
fi
case "$MARKER" in ''|*[!A-Za-z0-9_-]*) die "CCC_FLEET_SSH_MARKER must match [A-Za-z0-9_-]+" ;; esac
MB="# >>> $MARKER >>>"
ME="# <<< $MARKER <<<"

# Same strip program as the original script: drop every line from a begin
# marker through its end marker, print everything else unchanged.
AWK_STRIP='$0==mb{skip=1} !skip{print} $0==me{skip=0}'
# Fail (exit 1) on a begin inside a block, an end outside one, or an
# unterminated block — stripping such a file could drop unrelated lines.
AWK_BALANCED='$0==mb{if(inb)bad=1; inb=1; next} $0==me{if(!inb)bad=1; inb=0} END{exit (bad||inb)?1:0}'

# ------------------------------------------------------------- node table
declare -a NAMES=()
declare -A PORT=() USERN=() HOST=()

load_nodes() {
  [ -f "$NODES_FILE" ] || die "node table not found: $NODES_FILE (create it or pass --nodes FILE; one '<name> <port> <user> [host]' per line)"
  [ -r "$NODES_FILE" ] || die "node table not readable: $NODES_FILE"
  local line lineno=0 bad=0 name port user host extra
  local -a f
  while IFS= read -r line || [ -n "$line" ]; do
    lineno=$((lineno + 1))
    line="${line%%#*}"
    read -r -a f <<< "$line"   # word split without globbing
    [ "${#f[@]}" -eq 0 ] && continue
    if [ "${#f[@]}" -lt 3 ] || [ "${#f[@]}" -gt 4 ]; then
      echo "$PROG: $NODES_FILE:$lineno: expected '<name> <port> <user> [host]', got ${#f[@]} field(s)" >&2
      bad=1; continue
    fi
    name="${f[0]}" port="${f[1]}" user="${f[2]}" host="${f[3]:-}" extra=""
    if ! [[ "$name" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
      extra="invalid name '$name'"
    elif ! [[ "$port" =~ ^[0-9]{1,5}$ ]] || [ "$port" -lt 1 ] || [ "$port" -gt 65535 ]; then
      extra="invalid port '$port'"
    elif ! [[ "$user" =~ ^[A-Za-z_][A-Za-z0-9._-]*$ ]]; then
      extra="invalid user '$user'"
    elif [ -n "$host" ] && ! [[ "$host" =~ ^[A-Za-z0-9][A-Za-z0-9.:_-]*$ ]]; then
      extra="invalid host '$host'"
    elif [ -n "${PORT[$name]+x}" ]; then
      extra="duplicate node '$name'"
    fi
    if [ -n "$extra" ]; then
      echo "$PROG: $NODES_FILE:$lineno: $extra" >&2
      bad=1; continue
    fi
    NAMES+=("$name"); PORT[$name]="$port"; USERN[$name]="$user"; HOST[$name]="$host"
  done < "$NODES_FILE"
  [ "$bad" = 0 ] || die "invalid node table: $NODES_FILE (no action taken)"
  [ "${#NAMES[@]}" -gt 0 ] || die "node table has no nodes: $NODES_FILE"
}

# Fill HOST[] for every node without an explicit host from one
# `tailscale status --json` snapshot. Any miss or ambiguity aborts the run.
resolve_hosts() {
  local need=() n
  for n in "${NAMES[@]}"; do [ -n "${HOST[$n]}" ] || need+=("$n"); done
  [ "${#need[@]}" -gt 0 ] || return 0
  command -v python3 >/dev/null 2>&1 || die "python3 is required to read 'tailscale status --json'"
  local status out rc
  status=$("$TAILSCALE_BIN" status --json 2>/dev/null) || die "'$TAILSCALE_BIN status --json' failed; set <host> in $NODES_FILE or fix tailscale"
  out=$(printf '%s' "$status" | python3 -I -c '
import json, sys
try:
    st = json.load(sys.stdin)
except ValueError:
    print("BADJSON"); sys.exit(0)
devs = []
if isinstance(st.get("Self"), dict):
    devs.append(st["Self"])
peers = st.get("Peer") or {}
if isinstance(peers, dict):
    devs.extend(p for p in peers.values() if isinstance(p, dict))
for want in sys.argv[1:]:
    w = want.lower()
    hits = {}
    for d in devs:
        host = str(d.get("HostName") or "").lower()
        dns = str(d.get("DNSName") or "").lower().split(".")[0]
        if w in (host, dns):
            ips = [str(i) for i in (d.get("TailscaleIPs") or [])]
            key = d.get("PublicKey") or d.get("ID") or ",".join(ips)
            hits[key] = ips
    if not hits:
        print("NOTFOUND", want)
    elif len(hits) > 1:
        print("AMBIGUOUS", want, len(hits))
    else:
        ips = next(iter(hits.values()))
        v4 = [i for i in ips if "." in i and ":" not in i]
        pick = (v4 or ips or [""])[0]
        print("OK", want, pick) if pick else print("NOIP", want)
' "${need[@]}")
  rc=0
  local kind name val
  while read -r kind name val; do
    case "$kind" in
      OK) HOST[$name]="$val" ;;
      NOTFOUND) echo "$PROG: node '$name' not found in tailscale status (match by HostName/DNSName); set <host> in $NODES_FILE" >&2; rc=1 ;;
      AMBIGUOUS) echo "$PROG: node '$name' is ambiguous in tailscale status ($val devices match); remove the stale device or set <host> in $NODES_FILE" >&2; rc=1 ;;
      NOIP) echo "$PROG: node '$name' has no tailnet address in tailscale status" >&2; rc=1 ;;
      BADJSON) echo "$PROG: 'tailscale status --json' did not return JSON" >&2; rc=1 ;;
    esac
  done <<< "$out"
  for n in "${need[@]}"; do
    [ -n "${HOST[$n]}" ] || rc=1
  done
  [ "$rc" = 0 ] || die "host resolution failed (no action taken)"
}

# ---------------------------------------------------------------- remote
sshx() { # sshx <node> <remote command>
  timeout 45 "$SSH_BIN" -o BatchMode=yes -o ConnectTimeout=12 "$1" "$2" </dev/null
}

work_init() { (umask 077; mkdir -p "$WORK") || die "cannot create work dir: $WORK"; }

phase_collect() {
  work_init
  local n pk fails=0
  : > "$WORK/fleet_pubkeys"
  for n in "${NAMES[@]}"; do
    # Only the .pub is ever read; the private key is created in place if
    # missing and never printed, copied or transferred.
    pk=$(sshx "$n" 'mkdir -p ~/.ssh && chmod 700 ~/.ssh
      if [ ! -f ~/.ssh/id_ed25519_fleet ]; then
        ssh-keygen -q -t ed25519 -f ~/.ssh/id_ed25519_fleet -N "" -C "'"$n"'-fleet-'"$KEY_DATE"'" </dev/null >/dev/null
      fi
      cat ~/.ssh/id_ed25519_fleet.pub')
    if [[ "$pk" =~ ^ssh-ed25519\ [A-Za-z0-9+/=]+(\ [^[:cntrl:]]*)?$ ]]; then
      printf '%s\n' "$pk" >> "$WORK/fleet_pubkeys"
      echo "COLLECT-OK $n"
    else
      echo "COLLECT-FAIL $n"; fails=$((fails + 1))
    fi
  done
  [ "$fails" = 0 ]
}

phase_scan() {
  work_init
  local n out fails=0
  : > "$WORK/fleet_known_hosts"
  for n in "${NAMES[@]}"; do
    out=$("$SSH_KEYSCAN_BIN" -T 10 -p "${PORT[$n]}" -t ed25519 "${HOST[$n]}" 2>/dev/null | grep -v '^#')
    if [ -n "$out" ]; then
      printf '%s\n' "$out" >> "$WORK/fleet_known_hosts"
      echo "SCAN-OK $n"
    else
      echo "SCAN-FAIL $n"; fails=$((fails + 1))
    fi
  done
  [ "$fails" = 0 ]
}

phase_build() {
  local f n
  for f in fleet_pubkeys fleet_known_hosts; do
    [ -f "$WORK/$f" ] || { echo "BUILD-FAIL missing $WORK/$f (run collect and scan first)"; return 1; }
  done
  local npk nhk
  npk=$(grep -c '^ssh-ed25519 ' "$WORK/fleet_pubkeys")
  nhk=$(grep -cv '^[[:space:]]*$' "$WORK/fleet_known_hosts")
  if [ "$npk" != "${#NAMES[@]}" ] || [ "$nhk" != "${#NAMES[@]}" ]; then
    if [ "$ALLOW_PARTIAL" = 1 ]; then
      echo "BUILD-WARN partial: $npk pubkeys, $nhk hostkeys for ${#NAMES[@]} nodes (--allow-partial)"
    else
      echo "BUILD-FAIL partial: $npk pubkeys, $nhk hostkeys for ${#NAMES[@]} nodes; pushing would drop the missing nodes from every file (re-run collect/scan, or --allow-partial)"
      return 1
    fi
  fi
  { echo "$MB"
    cat "$WORK/fleet_pubkeys"
    echo "$ME"; } > "$WORK/block_authorized_keys"
  { echo "$MB"
    cat "$WORK/fleet_known_hosts"
    echo "$ME"; } > "$WORK/block_known_hosts"
  { echo "$MB"
    for n in "${NAMES[@]}"; do
      echo "Host $n"
      echo "    HostName ${HOST[$n]}"
      echo "    Port ${PORT[$n]}"
      echo "    User ${USERN[$n]}"
      echo "    IdentityFile ~/.ssh/id_ed25519_fleet"
      echo "    StrictHostKeyChecking accept-new"
      echo ""
    done
    echo "$ME"; } > "$WORK/block_config"
  echo "BUILD-OK $npk pubkeys, $nhk hostkeys"
}

push_block() { # push_block <node> <file under ~/.ssh> <local block> <mode> [prepend]
  local n="$1" rf="$2" lb="$3" mode="$4" pos="${5:-append}" blk
  blk=$(base64 -w0 "$lb") || return 1
  sshx "$n" 'set -u
    umask 077
    RF="$HOME/.ssh/'"$rf"'"; MB='"'$MB'"'; ME='"'$ME'"'
    mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh" || exit 1
    touch "$RF" || exit 1
    if ! awk -v mb="$MB" -v me="$ME" '"'$AWK_BALANCED'"' "$RF"; then
      echo "unbalanced marker block in $RF; left unchanged" >&2; exit 3
    fi
    B="$RF.bak-'"$STAMP"'"; i=0
    while [ -e "$B" ]; do i=$((i + 1)); B="$RF.bak-'"$STAMP"'.$i"; done
    cp -p "$RF" "$B" || exit 1
    awk -v mb="$MB" -v me="$ME" '"'$AWK_STRIP'"' "$RF" > "$RF.stripped" || exit 1
    if [ '"$pos"' = prepend ]; then
      printf "%s" '"'$blk'"' | base64 -d > "$RF.blk" || exit 1
      cat "$RF.blk" "$RF.stripped" > "$RF.tmp" || exit 1
      rm -f "$RF.blk"
    else
      cat "$RF.stripped" > "$RF.tmp" || exit 1
      printf "%s" '"'$blk'"' | base64 -d >> "$RF.tmp" || exit 1
    fi
    rm -f "$RF.stripped"
    chmod '"$mode"' "$RF.tmp" && mv -f "$RF.tmp" "$RF"'
}

# Read-only twin of push_block: fetch the current file, apply the same strip
# and block locally, report the difference. Nothing is written on the node.
dry_block() { # dry_block <node> <file under ~/.ssh> <local block> <mode> [prepend]
  local n="$1" rf="$2" lb="$3" mode="$4" pos="${5:-append}"
  local cur="$DRY_TMP/cur" new="$DRY_TMP/new" stripped="$DRY_TMP/stripped"
  sshx "$n" 'f="$HOME/.ssh/'"$rf"'"; if [ -f "$f" ]; then cat "$f"; fi' > "$cur" || return 1
  if ! awk -v mb="$MB" -v me="$ME" "$AWK_BALANCED" "$cur"; then
    echo "PUSH-DRYRUN-REFUSE $n ~/.ssh/$rf (unbalanced marker block)"; return 1
  fi
  awk -v mb="$MB" -v me="$ME" "$AWK_STRIP" "$cur" > "$stripped" || return 1
  if [ "$pos" = prepend ]; then cat "$lb" "$stripped" > "$new"; else cat "$stripped" "$lb" > "$new"; fi
  if cmp -s "$cur" "$new"; then
    echo "PUSH-DRYRUN-UNCHANGED $n ~/.ssh/$rf"
  else
    local d add del
    d=$(diff -u "$cur" "$new" | tail -n +3)
    add=$(grep -c '^+' <<< "$d"); del=$(grep -c '^-' <<< "$d")
    echo "PUSH-DRYRUN-CHANGE $n ~/.ssh/$rf (+$add -$del, mode $mode, backup .bak-$STAMP)"
    printf '    %s\n' "${d//$'\n'/$'\n    '}"
  fi
}

apply_block() {
  if [ "$DRY_RUN" = 1 ]; then dry_block "$@"; else push_block "$@"; fi
}

phase_push() {
  local f n ok fails=0
  for f in block_authorized_keys block_known_hosts block_config; do
    [ -f "$WORK/$f" ] || { echo "PUSH-FAIL missing $WORK/$f (run build first)"; return 1; }
  done
  if [ "$DRY_RUN" = 1 ]; then
    DRY_TMP=$(mktemp -d "${TMPDIR:-/tmp}/fleet-ssh-mesh.XXXXXX") || return 1
    trap 'rm -rf "$DRY_TMP"' EXIT
  fi
  for n in "${NAMES[@]}"; do
    ok=1
    apply_block "$n" authorized_keys "$WORK/block_authorized_keys" 600 || ok=0
    apply_block "$n" known_hosts     "$WORK/block_known_hosts"     644 || ok=0
    apply_block "$n" config          "$WORK/block_config"          600 prepend || ok=0
    if [ "$DRY_RUN" = 1 ]; then
      [ "$ok" = 1 ] || { echo "PUSH-DRYRUN-FAIL $n"; fails=$((fails + 1)); }
    elif [ "$ok" = 1 ]; then
      echo "PUSH-OK $n"
    else
      echo "PUSH-FAIL $n"; fails=$((fails + 1))
    fi
  done
  [ "$fails" = 0 ]
}

phase_verify() {
  local src t targets res fails=0
  for src in "${NAMES[@]}"; do
    targets=""
    for t in "${NAMES[@]}"; do [ "$t" = "$src" ] || targets="$targets $t"; done
    res=$(timeout 400 "$SSH_BIN" -o BatchMode=yes -o ConnectTimeout=12 "$src" 'for t in '"$targets"'; do
        r=$(timeout 15 ssh -o BatchMode=yes -o ConnectTimeout=8 "$t" "echo ok" </dev/null 2>/dev/null)
        [ "$r" = ok ] && printf "%s " "$t" || printf "FAIL:%s " "$t"
      done' </dev/null)
    echo "VERIFY $src -> ${res:-FAIL:unreachable}"
    case " ${res:-FAIL:} " in *" FAIL:"*) fails=$((fails + 1)) ;; esac
  done
  [ "$fails" = 0 ]
}

load_nodes
# Resolve every address before the first remote action, so a miss or an
# ambiguous device stops `all` before collect touches any node.
case "$PHASE" in scan|build|all) resolve_hosts ;; esac
case "$PHASE" in
  collect) phase_collect ;;
  scan)    phase_scan ;;
  build)   phase_build ;;
  push)    phase_push ;;
  verify)  phase_verify ;;
  all)     phase_collect && phase_scan && phase_build && phase_push ;;
esac
rc=$?
[ "$rc" = 0 ] || exit 1
exit 0
