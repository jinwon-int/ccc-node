#!/usr/bin/env bash
# Hermetic contract for the fleet watch's shared ssh connection (#2766): ssh is
# a recording stub, no node is contacted and no real connection is opened.
set -euo pipefail
pass=0
assert() {
  "$@"
  pass=$((pass + 1))
}
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP_BASE="${TMPDIR:-$(dirname "$ROOT")}"
mkdir -p "$TMP_BASE"
TMP="$(mktemp -d "$TMP_BASE/fleet-ssh-reuse-test.XXXXXX")"
# The code under test puts its sockets under TMPDIR and skips reuse when the
# path would not fit a unix socket, so the cases that expect reuse get a short
# private TMPDIR of their own.
SHORT_BASE="${TMPDIR:-/tmp}"
[ "${#SHORT_BASE}" -le 40 ] || SHORT_BASE=/tmp
SHORT="$(mktemp -d "$SHORT_BASE/fsr.XXXXXX")"
trap 'rm -rf "$TMP" "$SHORT"' EXIT
HELPER="$ROOT/scripts/lib/fleet-ssh-reuse.sh"

# ---- the helper on its own -------------------------------------------------
# start: a private directory of this run's own, exported for the channel watches.
out=$(TMPDIR="$SHORT" bash -c '. "$1"; fleet_ssh_reuse_start; printf "%s\n" "$CCC_FLEET_SSH_MUX_DIR"
  fleet_ssh_reuse_opts one 0; printf "%s\n" "${FLEET_SSH_REUSE[*]}"
  fleet_ssh_reuse_opts one 1; printf "%s\n" "${FLEET_SSH_REUSE[*]}"
  ls -ld "$CCC_FLEET_SSH_MUX_DIR" | cut -c1-10
  export -p | grep -c "CCC_FLEET_SSH_MUX_DIR="
  d=$CCC_FLEET_SSH_MUX_DIR; fleet_ssh_reuse_stop; [ -e "$d" ] && echo left || echo removed
  fleet_ssh_reuse_opts one 0; printf "%s|%s\n" "${CCC_FLEET_SSH_MUX_DIR:-unset}" "${#FLEET_SSH_REUSE[@]}"' _ "$HELPER")
dir=$(sed -n 1p <<< "$out")
assert test "${dir%/*}" = "$SHORT"
assert test "$(sed -n 2p <<< "$out")" = "-o ControlMaster=auto -o ControlPath=$dir/one -o ControlPersist=120"
# A retry never rides (or opens) the shared connection.
assert test "$(sed -n 3p <<< "$out")" = "-o ControlMaster=no -o ControlPath=none"
assert test "$(sed -n 4p <<< "$out")" = "drwx------"
assert test "$(sed -n 5p <<< "$out")" = 1
assert test "$(sed -n 6p <<< "$out")" = removed
# After stop nothing points at the removed directory.
assert test "$(sed -n 7p <<< "$out")" = "unset|0"

# Off switch: no directory, no options, and an inherited directory is dropped.
mkdir -m 700 "$SHORT/inherited"
out=$(TMPDIR="$SHORT" CCC_FLEET_SSH_REUSE=0 CCC_FLEET_SSH_MUX_DIR="$SHORT/inherited" bash -c '. "$1"
  fleet_ssh_reuse_start; fleet_ssh_reuse_opts one 0
  printf "%s|%s\n" "${CCC_FLEET_SSH_MUX_DIR:-unset}" "${#FLEET_SSH_REUSE[@]}"' _ "$HELPER")
assert test "$out" = "unset|0"
assert test "$(find "$SHORT" -maxdepth 1 -name 'ccc-fleet-ssh.*' | wc -l)" = 0

# start never adopts a directory named by the environment.
out=$(TMPDIR="$SHORT" CCC_FLEET_SSH_MUX_DIR="$SHORT/inherited" bash -c '. "$1"
  fleet_ssh_reuse_start; printf "%s\n" "$CCC_FLEET_SSH_MUX_DIR"; fleet_ssh_reuse_stop' _ "$HELPER")
assert test "$out" != "$SHORT/inherited"
assert test -d "$SHORT/inherited"

# A TMPDIR ssh could not take as a ControlPath (a space, a % token) or that
# does not exist: no reuse, and never an error.
mkdir -p "$TMP/has space" "$TMP/pct%h"
for bad in "$TMP/has space" "$TMP/pct%h" "$TMP/missing"; do
  out=$(TMPDIR="$bad" bash -c '. "$1"; fleet_ssh_reuse_start; rc=$?; fleet_ssh_reuse_opts one 0
    printf "%s|%s|%s\n" "$rc" "${CCC_FLEET_SSH_MUX_DIR:-unset}" "${#FLEET_SSH_REUSE[@]}"' _ "$HELPER")
  assert test "$out" = "0|unset|0"
done
assert test "$(find "$TMP/has space" "$TMP/pct%h" -mindepth 1 | wc -l)" = 0

# opts for a channel watch: the directory comes from the environment, so it is
# used only when it is the caller's own, private, and not a symlink.
opts() { # <dir> <node> [<attempt>]
  CCC_FLEET_SSH_MUX_DIR="$1" bash -c '. "$1"; fleet_ssh_reuse_opts "$2" "$3"
    printf "%s\n" "${FLEET_SSH_REUSE[*]}"' _ "$HELPER" "$2" "${3:-0}"
}
mkdir -m 700 "$SHORT/priv"
assert test "$(opts "$SHORT/priv" one)" = "-o ControlMaster=auto -o ControlPath=$SHORT/priv/one -o ControlPersist=120"
assert test "$(opts "$SHORT/priv" user@host-1.example)" = "-o ControlMaster=auto -o ControlPath=$SHORT/priv/user@host-1.example -o ControlPersist=120"
mkdir -m 755 "$SHORT/shared"
assert test -z "$(opts "$SHORT/shared" one)"
mkdir -m 770 "$SHORT/group"
assert test -z "$(opts "$SHORT/group" one)"
ln -s "$SHORT/priv" "$SHORT/link"
assert test -z "$(opts "$SHORT/link" one)"
assert test -z "$(opts "$SHORT/absent" one)"
assert test -z "$(opts "" one)"
# The node name becomes a file name: anything but a plain name is probed
# without reuse (the ssh call itself is unchanged for it).
for node in 'a/b' '../x' '-oProxyCommand=x' 'a%h' 'a b' ''; do
  assert test -z "$(opts "$SHORT/priv" "$node")"
done
# A socket path too long for a unix socket would make ssh fail outright.
long=$(printf 'n%.0s' $(seq 1 100))
assert test -z "$(opts "$SHORT/priv" "$long")"

# stop closes exactly the sockets that exist, then removes the directory.
cat > "$TMP/ssh-close" <<'SH'
#!/bin/sh
printf '%s\n' "$*" >> "$CALLS_FILE"
SH
chmod +x "$TMP/ssh-close"
: > "$TMP/close-calls"
out=$(TMPDIR="$SHORT" CALLS_FILE="$TMP/close-calls" CCC_FLEET_SSH="$TMP/ssh-close" bash -c '. "$1"
  fleet_ssh_reuse_start; d=$CCC_FLEET_SSH_MUX_DIR
  python3 -c "import socket,sys; socket.socket(socket.AF_UNIX).bind(sys.argv[1])" "$d/one"
  : > "$d/not-a-socket"
  fleet_ssh_reuse_stop; fleet_ssh_reuse_stop
  printf "%s\n" "$d"; [ -e "$d" ] && echo left || echo removed' _ "$HELPER")
dir=$(sed -n 1p <<< "$out")
assert test "$(sed -n 2p <<< "$out")" = removed
assert test "$(wc -l < "$TMP/close-calls")" = 1
assert grep -qx -- "-o BatchMode=yes -o ControlPath=$dir/one -O exit one" "$TMP/close-calls"
# No shared connection was opened: no extra ssh call at the end of a run.
: > "$TMP/close-calls"
TMPDIR="$SHORT" CALLS_FILE="$TMP/close-calls" CCC_FLEET_SSH="$TMP/ssh-close" bash -c '. "$1"
  fleet_ssh_reuse_start; fleet_ssh_reuse_stop' _ "$HELPER"
assert test "$(wc -l < "$TMP/close-calls")" = 0

# ---- a whole run -----------------------------------------------------------
# The stub answers each of the three probes and records, per call, which probe
# it was, the node, the Control* options it was given and the socket
# directory's mode at that moment.
STUB="$TMP/ssh"
cat > "$STUB" <<STUBEOF
#!$(command -v bash)
node=""; ctl=""; skip=0; close=0
for a in "\$@"; do
  case "\$a" in Control*) ctl="\$ctl \$a" ;; esac
  if [ "\$skip" = 1 ]; then skip=0; [ "\$a" != exit ] || close=1; continue; fi
  case "\$a" in
    -o|-O) skip=1 ;;
    -*) ;;
    *) [ -n "\$node" ] || node="\$a" ;;
  esac
done
body=\$(cat)
case "\$body" in
  *HANDE_STATUS*) kind=hande ;;
  *MATRIX_STATUS*) kind=matrix ;;
  *) kind=bridge ;;
esac
[ "\$close" = 0 ] || kind=close
dirmode=-
case "\$ctl" in *ControlPath=/*) p=\${ctl##*ControlPath=}; p=\${p%% *}; dirmode=\$(ls -ld "\${p%/*}" 2>/dev/null | cut -c1-10) ;; esac
printf '%s %s%s mode=%s\n' "\$kind" "\$node" "\$ctl" "\$dirmode" >> "\$CALLS_FILE"
# <node>.<kind>.fail holds how many more calls end as an ssh transport failure.
f="\$REPLY_DIR/\$node.\$kind.fail"
if [ -f "\$f" ]; then
  left=\$(cat "\$f")
  if [ "\$left" -gt 0 ]; then printf '%s\n' \$((left - 1)) > "\$f"; exit 255; fi
fi
case "\$kind" in
  hande) printf 'HANDE_STATUS=OK\nHANDE_REASON=available\nPROBE_COMPLETE=1\n' ;;
  matrix) printf 'MATRIX_STATUS=RETIRED\nMATRIX_REASON=frontends\nPROBE_COMPLETE=1\n' ;;
  bridge) printf 'RUNTIME=/opt/ccc-node\nAVAIL=yes\nUNIT=/opt/ccc-node\nPROBE_COMPLETE=1\n' ;;
esac
STUBEOF
chmod +x "$STUB"
mkdir -p "$TMP/reply"
sweep() { # <nodes> [VAR=value ...]
  : > "$TMP/calls"
  env -u CCC_FLEET_SSH_MUX_DIR -u CCC_FLEET_SSH_REUSE -u CCC_FLEET_MATRIX -u CCC_FLEET_HANDE \
    TMPDIR="$SHORT" REPLY_DIR="$TMP/reply" CALLS_FILE="$TMP/calls" CCC_FLEET_NODES="$1" \
    CCC_FLEET_SSH="$STUB" CCC_FLEET_SELF=_never_ CCC_FLEET_RETRY_DELAY=0 \
    CCC_FLEET_HANDE_RETRY_DELAY=0 CCC_FLEET_MATRIX_RETRY_DELAY=0 \
    CCC_FLEET_MEMORY_STATE="$TMP/mem/memory-degraded.tsv" "${@:2}" \
    bash "${SWEEP:-$ROOT/scripts/fleet-bridge-watch.sh}"
}

# Default run: the 「한데」 watch goes first (it allows the handshake 20s, the
# Matrix watch 10s), and all three probes of a node name the same socket.
set +e; out=$(sweep 'one two'); rc=$?; set -e
assert test "$rc" = 0
assert test "$(cut -d' ' -f1,2 "$TMP/calls" | tr '\n' ',')" = \
  'hande one,hande two,matrix one,matrix two,bridge one,bridge two,'
dir=$(sed -n 's/.* ControlPath=\(.*\)\/one .*/\1/p' "$TMP/calls" | sort -u)
assert test "$(wc -l <<< "$dir")" = 1
assert test "${dir%/*}" = "$SHORT"
for kind in hande matrix bridge; do
  for node in one two; do
    assert grep -qx "$kind $node ControlMaster=auto ControlPath=$dir/$node ControlPersist=120 mode=drwx------" "$TMP/calls"
  done
done
# The verdict rows follow the same order, and the directory is gone afterwards.
assert test "$(grep -n 'channel=hande' <<< "$out" | tail -1 | cut -d: -f1)" -lt \
  "$(grep -n 'channel=matrix' <<< "$out" | head -1 | cut -d: -f1)"
assert grep -qx 'OK one channel=hande reason=available' <<< "$out"
assert grep -qx 'RETIRED two channel=matrix reason=frontends' <<< "$out"
assert grep -q '^OK one (/opt/ccc-node)' <<< "$out"
assert test ! -e "$dir"
assert test "$(find "$SHORT" -maxdepth 1 -name 'ccc-fleet-ssh.*' | wc -l)" = 0

# A retry connects on its own, on every channel: the first call may ride the
# shared connection, the second is told not to.
for kind in hande matrix bridge; do printf '1\n' > "$TMP/reply/one.$kind.fail"; done
set +e; out=$(sweep 'one' CCC_FLEET_RETRIES=1); rc=$?; set -e
assert test "$rc" = 0
for kind in hande matrix bridge; do
  assert test "$(grep -c "^$kind one " "$TMP/calls")" = 2
  assert test "$(grep "^$kind one " "$TMP/calls" | sed -n 1p | grep -c 'ControlMaster=auto ControlPath=/')" = 1
  assert test "$(grep "^$kind one " "$TMP/calls" | sed -n 2p)" = "$kind one ControlMaster=no ControlPath=none mode=-"
done
assert test "$(grep -c UNREACHABLE <<< "$out")" = 0
rm -f "$TMP"/reply/*.fail

# Off switch: every ssh call is what it was before (no Control option at all),
# and nothing is created under TMPDIR.
set +e; out=$(sweep 'one two' CCC_FLEET_SSH_REUSE=0); rc=$?; set -e
assert test "$rc" = 0
assert test "$(wc -l < "$TMP/calls")" = 6
assert test "$(grep -c Control "$TMP/calls")" = 0
# An inherited socket directory is not used either.
set +e; out=$(sweep 'one' CCC_FLEET_SSH_REUSE=0 CCC_FLEET_SSH_MUX_DIR="$SHORT/priv"); rc=$?; set -e
assert test "$rc" = 0
assert test "$(grep -c Control "$TMP/calls")" = 0

# An unusable TMPDIR means no reuse, not a failed run.
set +e; out=$(sweep 'one' TMPDIR="$TMP/has space"); rc=$?; set -e
assert test "$rc" = 0
assert test "$(wc -l < "$TMP/calls")" = 3
assert test "$(grep -c Control "$TMP/calls")" = 0

# A node whose name cannot be a socket file name is probed without reuse while
# the others keep it.
set +e; out=$(sweep 'one a%h'); rc=$?; set -e
assert test "$rc" = 0
assert test "$(grep -c '^[a-z]* one ControlMaster=auto' "$TMP/calls")" = 3
assert test "$(grep -c '^[a-z]* a%h mode=-$' "$TMP/calls")" = 3

# The channel watches on their own (no sweep around them) connect as before,
# and take a socket directory from the environment only when it is private.
standalone() { # <script> [VAR=value ...]
  : > "$TMP/calls"
  env -u CCC_FLEET_SSH_MUX_DIR -u CCC_FLEET_SSH_REUSE REPLY_DIR="$TMP/reply" CALLS_FILE="$TMP/calls" \
    CCC_FLEET_NODES=one CCC_FLEET_SSH="$STUB" CCC_FLEET_SELF=_never_ "${@:2}" \
    bash "$ROOT/scripts/$1"
}
for script in fleet-hande-watch.sh fleet-matrix-watch.sh; do
  standalone "$script" >/dev/null
  assert test "$(grep -c Control "$TMP/calls")" = 0
  standalone "$script" CCC_FLEET_SSH_MUX_DIR="$SHORT/shared" >/dev/null
  assert test "$(grep -c Control "$TMP/calls")" = 0
  standalone "$script" CCC_FLEET_SSH_MUX_DIR="$SHORT/priv" >/dev/null
  assert test "$(grep -c "ControlMaster=auto ControlPath=$SHORT/priv/one ControlPersist=120" "$TMP/calls")" = 1
  # The off switch wins over a directory in the environment.
  standalone "$script" CCC_FLEET_SSH_MUX_DIR="$SHORT/priv" CCC_FLEET_SSH_REUSE=0 >/dev/null
  assert test "$(grep -c Control "$TMP/calls")" = 0
done

# A copy of the watch scripts without the helper still runs, without reuse.
mkdir -p "$TMP/copy/scripts"
cp "$ROOT"/scripts/fleet-bridge-watch.sh "$ROOT"/scripts/fleet-hande-watch.sh \
  "$ROOT"/scripts/fleet-matrix-watch.sh "$ROOT"/scripts/fleet_*_probe.py \
  "$ROOT"/scripts/fleet_watch_metadata.py "$TMP/copy/scripts/"
set +e; out=$(SWEEP="$TMP/copy/scripts/fleet-bridge-watch.sh" sweep 'one' CCC_FLEET_SSH_MUX_DIR="$SHORT/priv"); rc=$?; set -e
assert test "$rc" = 0
assert test "$(wc -l < "$TMP/calls")" = 3
assert test "$(grep -c Control "$TMP/calls")" = 0
assert grep -qx 'OK one channel=hande reason=available' <<< "$out"

printf 'PASS=%s FAIL=0\n' "$pass"
