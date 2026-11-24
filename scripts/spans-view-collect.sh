#!/usr/bin/env bash
# Hub-side daily span view (#2858, item 4): read each node's body-free rollup
# summary for ONE day over a read-only stdin probe, then build the static pages.
#
#   CCC_FLEET_NODES="a b c" spans-view-collect.sh --out-dir DIR [--date YYYY-MM-DD] [--collect-only]
#
# Same shape as fleet-matrix-watch.sh: `ssh <node> python3 - <day> < probe`, one
# retry when an attempt fails or answers nothing, and the last attempt's exit
# decides the verdict — 255 is an ssh transport failure (UNREACHABLE), 124 is
# a node that accepted the connection but did not finish in time (UNVERIFIED),
# whether or not some output arrived before the failure. Inside
# a fleet sweep the first attempt rides the connection that sweep already
# opened (scripts/lib/fleet-ssh-reuse.sh); run by itself it connects on its own.
#
# Read-only on the nodes: the probe opens one summary file and prints it. No
# node cron entry, file, or raw span is touched or copied. The answers are
# staged in a private folder on the hub, checked and rendered by
# scripts/spans-view.py, and the staging folder is removed on exit.
set -u
# The node list is split on blanks below; a `*` in it must stay a (refused) name.
set -f

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PROBE="$ROOT/scripts/spans_view_probe.py"
VIEW="$ROOT/scripts/spans-view.py"
# No built-in node list (fleet node names may not spread, #1451): the timer unit
# passes the hub's list; a manual run names the nodes explicitly.
NODES="${CCC_FLEET_NODES:-}"
SSH_BIN="${CCC_FLEET_SSH:-ssh}"
SELF="${CCC_FLEET_SELF:-$(hostname -s 2>/dev/null || echo _none_)}"
STATE_DIR="${CCC_STATE_DIR:-$HOME/.claude/state}"
# Worst case 12 nodes x (2 x 20s + 2s) = 504s; the timer unit allows 900s.
LIMIT="${CCC_SPANS_VIEW_PROBE_TIMEOUT:-20}"
RETRIES="${CCC_SPANS_VIEW_RETRIES:-1}"
RETRY_DELAY="${CCC_SPANS_VIEW_RETRY_DELAY:-2}"
# One answer: an ASCII-escaped JSON line (the node caps the file at 256 KiB)
# plus three status lines. spans-view.py applies the same cap again.
MAX_BYTES=$((2 * 1024 * 1024))
case "$LIMIT" in ''|*[!0-9]*|????*) LIMIT=20 ;; esac
case "$RETRIES" in ''|*[!0-9]*) RETRIES=1 ;; esac
case "$RETRY_DELAY" in ''|*[!0-9]*) RETRY_DELAY=2 ;; esac
if [ "$LIMIT" -lt 1 ] || [ "$LIMIT" -gt 120 ]; then LIMIT=20; fi
[ "$RETRIES" -le 1 ] || RETRIES=1
[ "$RETRY_DELAY" -le 2 ] || RETRY_DELAY=2

OUT_DIR=''; DAY=''; COLLECT_ONLY=0
usage() { echo "usage: spans-view-collect.sh --out-dir DIR [--date YYYY-MM-DD] [--collect-only]" >&2; }
while [ $# -gt 0 ]; do
  case "$1" in
    --out-dir) [ -n "${2:-}" ] || { usage; exit 2; }; OUT_DIR="$2"; shift 2 ;;
    --date) [ -n "${2:-}" ] || { usage; exit 2; }; DAY="$2"; shift 2 ;;
    --collect-only) COLLECT_ONLY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage; exit 2 ;;
  esac
done
[ -n "$DAY" ] || DAY="$(TZ=Asia/Seoul date -d yesterday +%F 2>/dev/null)"
# The day is passed to the remote command line, so it must be digits and dashes only.
[[ "$DAY" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || { echo "spans-view-collect: bad --date" >&2; exit 2; }
if [ "$COLLECT_ONLY" = 0 ] && [ -z "$OUT_DIR" ]; then usage; exit 2; fi
if [ -z "$NODES" ]; then echo "spans-view-collect: CCC_FLEET_NODES is required (space-separated node list)" >&2; exit 2; fi
if [ ! -r "$PROBE" ] || [ ! -r "$VIEW" ]; then echo "spans-view-collect: probe or view script missing" >&2; exit 2; fi

# shellcheck source=scripts/lib/fleet-ssh-reuse.sh
. "$ROOT/scripts/lib/fleet-ssh-reuse.sh" 2>/dev/null || fleet_ssh_reuse_opts() { FLEET_SSH_REUSE=(); }

umask 077
mkdir -p "$STATE_DIR/spans-view/incoming" || { echo "spans-view-collect: cannot create staging root" >&2; exit 3; }
STAGE="$(mktemp -d "$STATE_DIR/spans-view/incoming/$DAY.XXXXXX")" || { echo "spans-view-collect: cannot create staging folder" >&2; exit 3; }
if [ "$COLLECT_ONLY" = 0 ]; then trap 'rm -rf "$STAGE"' EXIT; fi

SAFE_NODES=''
for node in $NODES; do
  # The node name becomes a file name here and an ssh destination below.
  [[ "$node" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] || { echo "spans-view-collect: skipping unsafe node name" >&2; continue; }
  SAFE_NODES="${SAFE_NODES:+$SAFE_NODES }$node"
  out="$STAGE/$node.out"
  attempt=0
  while :; do
    if [ "$node" = "$SELF" ]; then
      python3 "$PROBE" "$DAY" 2>/dev/null | head -c "$((MAX_BYTES + 1))" > "$out"; rc=${PIPESTATUS[0]}
    else
      fleet_ssh_reuse_opts "$node" "$attempt"
      timeout "$LIMIT" "$SSH_BIN" -o BatchMode=yes -o ConnectTimeout=8 ${FLEET_SSH_REUSE[@]+"${FLEET_SSH_REUSE[@]}"} \
        "$node" python3 - "$DAY" < "$PROBE" 2>/dev/null | head -c "$((MAX_BYTES + 1))" > "$out"; rc=${PIPESTATUS[0]}
    fi
    # Done when the probe exited cleanly with an answer. A failed attempt is
    # retried even if it printed something: a partial answer is not an answer.
    [ "$rc" = 0 ] && [ -s "$out" ] && break
    attempt=$((attempt + 1))
    [ "$attempt" -gt "$RETRIES" ] && break
    [ "$RETRY_DELAY" -gt 0 ] && sleep "$RETRY_DELAY"
  done
  size=$(wc -c < "$out" 2>/dev/null || echo 0)
  if [ "$size" -gt "$MAX_BYTES" ]; then
    transport=oversize
  elif [ "$node" != "$SELF" ] && [ "$rc" = 255 ]; then
    # The exit decides, with or without partial output (#2673, #2766).
    transport=unreachable
  elif [ "$node" != "$SELF" ] && [ "$rc" = 124 ]; then
    transport=timeout
  elif [ "$rc" != 0 ] || [ ! -s "$out" ]; then
    transport=error
  else
    transport=ok
  fi
  [ "$transport" = ok ] || : > "$out"
  printf 'transport=%s rc=%s\n' "$transport" "$rc" > "$STAGE/$node.meta"
done

if [ "$COLLECT_ONLY" = 1 ]; then
  echo "$STAGE"
  exit 0
fi
CCC_STATE_DIR="$STATE_DIR" python3 "$VIEW" build --date "$DAY" --nodes "$SAFE_NODES" --staging "$STAGE" --out-dir "$OUT_DIR"
