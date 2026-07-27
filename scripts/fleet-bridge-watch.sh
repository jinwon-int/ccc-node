#!/usr/bin/env bash
# fleet-bridge-watch — periodic fleet bridge health + boot-path check.
#
# Intended as an agent-cron command payload: exits nonzero when any node fails,
# which drives telegram-owner-on-failure notification.
#
# Two checks per node:
#   1. availability — the bridge answers "Bot status: available"
#   2. boot path    — the systemd unit that would restart the bridge points at
#                     the checkout the bridge is ACTUALLY serving from
#
# Every path is derived from the running process. Nodes hold several ccc-node
# checkouts (/opt, /root, /home/<user>) and which one is live differs per node
# and changes over time, so a hardcoded table goes stale silently — the check
# then passes against a path nobody serves from, or fails against a healthy
# node. This is the same class of defect the boot-path check itself exists to
# catch (yukson 2026-07-27).
#
# Env overrides (used by the tests):
#   CCC_FLEET_NODES  space-separated node list (default: canonical 12)
#   CCC_FLEET_SSH    ssh binary (default: ssh)
#   CCC_FLEET_SELF   node name to probe locally instead of over ssh
#   CCC_FLEET_DOCTOR set to 1 to also run ccc-doctor on every node and fail on
#                    drift. Off by default so the bridge check stays fast; the
#                    sweep is scheduled separately with its own timeout, since
#                    only 3 of 12 nodes have agent-cron and the rest would
#                    otherwise never be checked for harness drift.
set -u

NODES="${CCC_FLEET_NODES:-seoseo dungae sogyo nosuk bangtong yukson soonwook gwakga jingun gongmyoung gongyung daegyo}"
SSH_BIN="${CCC_FLEET_SSH:-ssh}"
SELF="${CCC_FLEET_SELF:-$(hostname -s 2>/dev/null || echo _none_)}"

# POSIX sh, runs on every node including Termux. Emits KEY=VALUE lines only.
read -r -d '' PROBE <<'PROBE_EOF' || true
# uid, not user: the `user` column truncates names longer than 8 characters
# ("gongmyoung" -> "gongmyo+"), and the truncated form is not a valid su target.
line=$(ps -eo uid=,command= 2>/dev/null | grep 'telegram_bot' | grep -- '--path' | grep -v grep | head -1)
if [ -z "$line" ]; then echo "RUNTIME=-"; echo "AVAIL=no"; echo "UNIT=-"; exit 0; fi
runuid=$(printf '%s' "$line" | awk '{print $1}')
runuser=$(id -nu "$runuid" 2>/dev/null || printf '%s' "$runuid")
cmd=$(printf '%s' "$line" | sed 's/^ *[0-9][0-9]* *//')
root=""
for tok in $cmd; do case "$tok" in */bridge/*) root=${tok%%/bridge/*}; break ;; esac; done
bpath=$(printf '%s' "$cmd" | awk '{for(i=1;i<NF;i++) if($i=="--path") {print $(i+1); exit}}')
[ -n "$root" ] || { echo "RUNTIME=-"; echo "AVAIL=no"; echo "UNIT=-"; exit 0; }
echo "RUNTIME=$root"

# availability — run start.sh as the account that owns the process
st=""
if [ "$(id -u)" = "$runuid" ]; then
  st=$("$root/bridge/start.sh" --path "${bpath:-$HOME}" --status 2>/dev/null)
else
  st=$(su - "$runuser" -c "'$root/bridge/start.sh' --path '${bpath:-~}' --status" 2>/dev/null)
fi
printf '%s' "$st" | grep -q 'Bot status: available' && echo "AVAIL=yes" || echo "AVAIL=no"

# boot path — first unit that declares an ExecStart wins
unit_root=""
for u in /etc/systemd/system/ccc-telegram-bridge.service /home/*/.config/systemd/user/ccc-telegram-bridge.service; do
  [ -f "$u" ] || continue
  exec_line=$(grep -m1 '^ExecStart=' "$u" 2>/dev/null | sed 's/^ExecStart=//')
  [ -n "$exec_line" ] || continue
  for tok in $exec_line; do case "$tok" in */bridge/*) unit_root=${tok%%/bridge/*}; break ;; esac; done
  [ -n "$unit_root" ] && break
done
echo "UNIT=${unit_root:--}"

# doctor (opt-in). Runs as the account that owns the bridge, against the claude
# dir that belongs to it — both derived above, never guessed.
if [ "${CCC_FLEET_DOCTOR:-0}" = "1" ] && [ -x "$root/scripts/ccc-doctor.sh" ]; then
  cdir="${bpath:-$HOME}/.claude"
  if [ "$(id -u)" = "$runuid" ]; then
    CCC_DOCTOR_CLAUDE_DIR="$cdir" timeout 60 "$root/scripts/ccc-doctor.sh" >/dev/null 2>&1
  else
    su - "$runuser" -c "CCC_DOCTOR_CLAUDE_DIR='$cdir' timeout 60 '$root/scripts/ccc-doctor.sh'" >/dev/null 2>&1
  fi
  echo "DOCTOR=$?"
else
  echo "DOCTOR=-"
fi
PROBE_EOF

fail=0
for node in $NODES; do
  # The flag is prepended to the piped script rather than passed as an ssh
  # argument: the remote command stays exactly `sh -s`, so nothing downstream
  # has to parse a modified argv.
  payload="CCC_FLEET_DOCTOR=${CCC_FLEET_DOCTOR:-0}
$PROBE"
  if [ "${CCC_FLEET_DOCTOR:-0}" = "1" ]; then node_budget=90; else node_budget=30; fi
  if [ "$node" = "$SELF" ]; then
    out=$(printf '%s' "$payload" | sh -s 2>/dev/null)
  else
    out=$(printf '%s' "$payload" | timeout "$node_budget" "$SSH_BIN" -o BatchMode=yes -o ConnectTimeout=8 "$node" sh -s 2>/dev/null)
  fi

  if [ -z "$out" ]; then
    echo "UNREACHABLE $node"; fail=1; continue
  fi

  avail=$(printf '%s\n' "$out" | sed -n 's/^AVAIL=//p' | head -1)
  runtime=$(printf '%s\n' "$out" | sed -n 's/^RUNTIME=//p' | head -1)
  unit=$(printf '%s\n' "$out" | sed -n 's/^UNIT=//p' | head -1)

  if [ "$avail" != "yes" ]; then
    echo "DOWN $node"; fail=1; continue
  fi

  # A unit pointing elsewhere is silent while the bridge is up: the next reboot
  # serves the stale checkout. Report it even though availability passed.
  if [ -n "$unit" ] && [ "$unit" != "-" ] && [ "$unit" != "$runtime" ]; then
    echo "BOOTPATH $node unit=$unit runtime=$runtime"; fail=1; continue
  fi

  doctor=$(printf '%s\n' "$out" | sed -n 's/^DOCTOR=//p' | head -1)
  # doctor exits nonzero on 교정가능/수동필요 findings; 경고 does not count.
  if [ -n "$doctor" ] && [ "$doctor" != "-" ] && [ "$doctor" != "0" ]; then
    echo "DRIFT $node doctor_exit=$doctor runtime=$runtime"; fail=1; continue
  fi

  echo "OK $node ($runtime)"
done
exit $fail
