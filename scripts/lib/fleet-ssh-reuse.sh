# shellcheck shell=bash
# One ssh connection per node for a whole fleet-watch run (#2766). Sourced by
# fleet-bridge-watch.sh, fleet-hande-watch.sh and fleet-matrix-watch.sh; it
# only defines functions and runs nothing by itself.
#
# A watch run used to open three ssh connections to every node, one per
# channel (Matrix, 「한데」, bridge), each a full key exchange and login under
# its own time limit. From a far watcher that handshake is almost the whole
# cost of a probe: measured 2026-11-13 from the T2 watcher (medians of 6), a
# probe to the three nodes on slow links took 3.9-4.6 s over a new connection
# and 0.9-1.6 s over one already open; to the eight other nodes 0.4-2.7 s
# against 0.1-0.8 s. The handshake is also the part that stalls: in the 740
# scheduled 15-minute runs of 2026-11-06..13 those three nodes produced all 81
# unanswered probe rows, and the Matrix pass — first in the run, with the tightest
# limit (10 s) — took 32 of them for a channel that is retired on every node
# but one.
#
# With reuse the first probe of a node opens the connection (OpenSSH
# ControlMaster=auto) and the later ones ride it, so a run pays one handshake
# per node instead of three, under the limit of whichever watch runs first.
#
# Rules that keep a shared connection from costing a verdict:
#   - Only the first attempt of a probe may open or ride it. A retry always
#     makes its own connection (ControlPath=none), exactly as before this
#     change, so a shared connection that went stale costs one attempt and
#     never the verdict. (When the process holding the connection does not
#     answer at all, ssh itself gives up on it after ConnectTimeout and
#     connects anew within the same attempt.)
#   - The sockets live in a directory this run creates (mode 0700, removed on
#     exit); a directory that is not the caller's own and private is ignored.
#   - Anything that cannot be used safely — a node name that is not a plain
#     file name, a socket path too long for a unix socket — means that node is
#     probed without reuse, never an error.
#   - CCC_FLEET_SSH_REUSE=0 turns it off: no option is added to any ssh call.
#
# fleet_ssh_reuse_start
#     The sweep that owns the run calls this once. Creates the socket
#     directory under ${TMPDIR:-/tmp} and exports CCC_FLEET_SSH_MUX_DIR for the
#     channel watches it starts. Quietly does nothing when reuse is off or the
#     directory cannot be made.
# fleet_ssh_reuse_opts <node> <attempt>
#     Sets the array FLEET_SSH_REUSE to the ssh options for this attempt
#     (attempt 0 is the first). Empty when CCC_FLEET_SSH_MUX_DIR is unset or
#     unusable, so a watch run on its own behaves as it always did.
# fleet_ssh_reuse_stop
#     Closes the connections this run opened and removes the directory. Safe
#     to call more than once, and from an EXIT trap.

# How long an idle shared connection stays open. A run walks the node list
# three times, so a node's connection sits idle for about one pass (seconds to
# a minute); one that expired is simply opened again. It also bounds what a
# killed run leaves behind.
FLEET_SSH_REUSE_PERSIST=120
FLEET_SSH_REUSE=()
FLEET_SSH_REUSE_OWNED=''
FLEET_SSH_REUSE_CHECKED=''

# A unix socket path holds 104 bytes on the smallest platform, and ssh binds a
# temporary name 17 bytes longer than the ControlPath before renaming it.
_fleet_ssh_reuse_fits() { # <dir> <node>
  [ $(( ${#1} + 1 + ${#2} + 17 )) -lt 104 ]
}

# The directory must be the caller's own, private, and not a symlink: a socket
# planted in a shared directory would receive the probe and answer for the node.
_fleet_ssh_reuse_dir_ok() { # <dir>
  case "$1" in ''|*[!A-Za-z0-9._/-]*) return 1 ;; esac
  [ -d "$1" ] && [ ! -L "$1" ] && [ -O "$1" ] || return 1
  case "$(ls -ld "$1" 2>/dev/null)" in drwx------*) return 0 ;; esac
  return 1
}

fleet_ssh_reuse_start() {
  [ "${CCC_FLEET_SSH_REUSE:-1}" != 0 ] || { unset CCC_FLEET_SSH_MUX_DIR; return 0; }
  [ -z "$FLEET_SSH_REUSE_OWNED" ] || return 0
  # Never adopt a directory named by the environment: this run's sockets go in
  # a directory it made itself.
  unset CCC_FLEET_SSH_MUX_DIR
  local dir
  dir=$(umask 077; mktemp -d "${TMPDIR:-/tmp}/ccc-fleet-ssh.XXXXXX" 2>/dev/null) || return 0
  if ! _fleet_ssh_reuse_dir_ok "$dir"; then
    rmdir "$dir" 2>/dev/null
    return 0
  fi
  FLEET_SSH_REUSE_OWNED=$dir
  export CCC_FLEET_SSH_MUX_DIR="$dir"
}

# FLEET_SSH_REUSE is the result, read by the sourcing script.
# shellcheck disable=SC2034
fleet_ssh_reuse_opts() { # <node> <attempt>
  FLEET_SSH_REUSE=()
  [ "${CCC_FLEET_SSH_REUSE:-1}" != 0 ] || return 0
  local dir=${CCC_FLEET_SSH_MUX_DIR:-}
  [ -n "$dir" ] || return 0
  if [ "$FLEET_SSH_REUSE_CHECKED" != "$dir" ]; then
    _fleet_ssh_reuse_dir_ok "$dir" || { unset CCC_FLEET_SSH_MUX_DIR; return 0; }
    FLEET_SSH_REUSE_CHECKED=$dir
  fi
  # The node name becomes the socket's file name.
  [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9._@-]*$ ]] || return 0
  _fleet_ssh_reuse_fits "$dir" "$1" || return 0
  if [ "${2:-0}" = 0 ]; then
    FLEET_SSH_REUSE=(-o ControlMaster=auto -o "ControlPath=$dir/$1"
                     -o "ControlPersist=$FLEET_SSH_REUSE_PERSIST")
  else
    FLEET_SSH_REUSE=(-o ControlMaster=no -o ControlPath=none)
  fi
}

fleet_ssh_reuse_stop() {
  local dir=$FLEET_SSH_REUSE_OWNED sock
  [ -n "$dir" ] || return 0
  FLEET_SSH_REUSE_OWNED=''
  FLEET_SSH_REUSE_CHECKED=''
  unset CCC_FLEET_SSH_MUX_DIR
  [ -d "$dir" ] || return 0
  for sock in "$dir"/*; do
    # Only a socket that exists is closed, so a run that opened no shared
    # connection makes no extra ssh call.
    [ -S "$sock" ] || continue
    timeout 3 "${CCC_FLEET_SSH:-ssh}" -o BatchMode=yes -o "ControlPath=$sock" \
      -O exit "${sock##*/}" >/dev/null 2>&1 || true
  done
  rm -rf "$dir"
}
