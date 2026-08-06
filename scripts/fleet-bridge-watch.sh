#!/usr/bin/env bash
# fleet-bridge-watch — periodic fleet bridge health + boot-path check.
#
# Intended as an agent-cron command payload: exits nonzero when any node fails,
# which drives telegram-owner-on-failure notification.
#
# Three checks per node:
#   1. availability — the bridge answers "Bot status: available"
#   2. canonical    — the checkout it serves from is one the fleet installs at,
#                     not a PR/issue work tree
#   3. boot path    — the systemd unit that would restart the bridge points at
#                     the checkout the bridge is ACTUALLY serving from
#
# Check 2 exists because check 3 is a comparison, and a comparison is silent
# when both sides are wrong together. On 2026-08-01 seoseo served its bridge
# from /work/agent-codebench/ccc-node-pr833 — a PR head that never reached main,
# five commits behind it — with the unit pointing at that same worktree, and the
# boot-path check called it healthy. It surfaced only through incidental doctor
# drift. Judge the runtime root on its own before comparing it to anything.
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
#   CCC_FLEET_RETRIES extra attempts per node on transport failure (default 2,
#                    capped at 5; only UNREACHABLE is retried — a node that
#                    answers, even DOWN, is judged on its single answer)
#   CCC_FLEET_RETRY_DELAY seconds between attempts (default 10, capped at 120)
set -u

NODES="${CCC_FLEET_NODES:-seoseo dungae sogyo nosuk bangtong yukson soonwook gwakga jingun gongmyoung gongyung daegyo}"
SSH_BIN="${CCC_FLEET_SSH:-ssh}"
SELF="${CCC_FLEET_SELF:-$(hostname -s 2>/dev/null || echo _none_)}"

# Checkout roots a node may legitimately serve from. Several are in use at once
# across the fleet (/opt on most, /root on yukson and gwakga, the Termux home on
# the phones), so this is a glob list rather than one path.
CANON_ROOTS="${CCC_FLEET_CANONICAL_ROOTS:-/opt/ccc-node /root/ccc-node /home/*/ccc-node /data/data/com.termux/files/home/ccc-node}"

# The basename has to match exactly. Work trees are created as siblings of the
# real checkout with a suffix — /root/ccc-node-840-terminal-stall, or
# /work/agent-codebench/ccc-node-pr833 — so a prefix or substring test would
# wave through the exact shape this check exists to catch.
# `set -f` is not incidental. Splitting the list without it makes the shell
# expand /home/*/ccc-node against THIS host's filesystem: on a watcher that has
# /home/ccc/ccc-node the pattern collapses to that one literal, and every other
# node's /home/<user>/ccc-node is then reported non-canonical. The paths being
# judged are remote, so no local filesystem may influence the verdict — these
# are patterns to match with, never paths to resolve.
is_canonical_root() {
  _root=$1 _hit=1
  set -f
  for _pat in $CANON_ROOTS; do
    # shellcheck disable=SC2254  # $_pat is a glob on purpose (/home/*/ccc-node)
    case "$_root" in $_pat) _hit=0; break ;; esac
  done
  set +f
  return $_hit
}

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

# gongmyoung dual-domain coherence (#980) — doctor-sweep only. gongmyoung is
# the fleet's only dual-domain node (gongmyoung user = bridge/runtime, root =
# A2A/system), and the "one node = one domain" assumption failing there caused
# repeat incidents (self-update cron lost wholesale, root-owned /opt checkout,
# decoy ccc-node dirs trapping resolve_repo heuristics). These checks are
# read-only; remediation is an operator decision. Single-domain nodes have no
# gongmyoung account, so they emit DUALDOMAIN=- and cost nothing.
if [ "${CCC_FLEET_DOCTOR:-0}" = "1" ] && id gongmyoung >/dev/null 2>&1 && [ -d /home/gongmyoung ]; then
  if [ "$(id -u)" != 0 ]; then
    # crontab/loginctl inspection needs root; say so instead of guessing.
    echo "DUALDOMAIN=skip(non-root)"
  else
    dd_fail=""
    dd_add() { dd_fail="${dd_fail:+${dd_fail},}$1"; }

    # 1. the gongmyoung crontab still carries self-update, wired to the user bus
    gcron=$(crontab -u gongmyoung -l 2>/dev/null || true)
    printf '%s\n' "$gcron" | grep -q 'ccc-self-update' || dd_add 'cron-self-update-missing'
    printf '%s\n' "$gcron" | grep -q 'XDG_RUNTIME_DIR' \
      && printf '%s\n' "$gcron" | grep -q 'DBUS_SESSION_BUS_ADDRESS' \
      || dd_add 'cron-bus-env-missing'

    # 2. /opt/ccc-node is gongmyoung-owned, clean, on main, with no root-owned
    #    git objects (a root-owned object is what flipped the 2026-08-05
    #    self-update registration to the non-serving repo)
    repo=/opt/ccc-node
    [ -d "$repo/.git" ] || dd_add 'repo-missing'
    [ "$(stat -c %U "$repo" 2>/dev/null || echo ?)" = "gongmyoung" ] || dd_add 'repo-not-gongmyoung-owned'
    [ -z "$(su - gongmyoung -c "git -C $repo status --porcelain" 2>/dev/null | head -1)" ] || dd_add 'repo-dirty'
    dd_branch=$(su - gongmyoung -c "git -C $repo rev-parse --abbrev-ref HEAD" 2>/dev/null || echo ?)
    [ "$dd_branch" = "main" ] || dd_add "repo-branch=$dd_branch"
    [ -z "$(find "$repo/.git" -user root -print -quit 2>/dev/null)" ] || dd_add 'git-root-owned-objects'

    # 3. the user unit and its manager are actually alive, and linger keeps
    #    them that way across logout
    uid_g=$(id -u gongmyoung 2>/dev/null || echo 1000)
    dd_bus="XDG_RUNTIME_DIR=/run/user/$uid_g DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$uid_g/bus"
    dd_unit=$(su - gongmyoung -c "env $dd_bus systemctl --user is-active ccc-telegram-bridge" 2>/dev/null || true)
    [ "$dd_unit" = "active" ] || dd_add "user-unit=${dd_unit:-unknown}"
    dd_mgr=$(systemctl is-active "user@$uid_g" 2>/dev/null || true)
    [ "$dd_mgr" = "active" ] || dd_add "user-manager=${dd_mgr:-unknown}"
    dd_linger=$(loginctl show-user gongmyoung 2>/dev/null | sed -n 's/^Linger=//p' | head -1)
    [ "$dd_linger" = "yes" ] || dd_add "linger=${dd_linger:-unknown}"

    # 4. no decoy ccc-node checkouts under the gongmyoung home (real dirs only;
    #    symlinks/tarballs/docs are not serving candidates)
    dd_decoys=0
    for d in /home/gongmyoung/ccc-node-*; do
      [ -d "$d" ] && [ ! -L "$d" ] || continue
      dd_decoys=$((dd_decoys + 1))
    done
    [ "$dd_decoys" = 0 ] || dd_add "decoy-dirs=$dd_decoys"

    if [ -z "$dd_fail" ]; then echo "DUALDOMAIN=ok"; else echo "DUALDOMAIN=fail $dd_fail"; fi
  fi
else
  echo "DUALDOMAIN=-"
fi
PROBE_EOF

# One unanswered probe is a transport blip, not a health signal: on 2026-07-31
# and 2026-08-01 single SSH failures paged UNREACHABLE for nodes that were fine
# minutes later, and a 42% failure rate taught the fleet to ignore the watch —
# the one real outage (daegyo 2026-08-06, #968) looked identical to the noise
# (#972). Retry transport failures with a delay so a blip never reaches the
# notification path; a persistent failure still ends UNREACHABLE after the
# retries, and a node that answers is never re-asked.
RETRIES="${CCC_FLEET_RETRIES:-2}"
case "$RETRIES" in ''|*[!0-9]*) RETRIES=2 ;; esac
[ "$RETRIES" -le 5 ] || RETRIES=5
RETRY_DELAY="${CCC_FLEET_RETRY_DELAY:-10}"
case "$RETRY_DELAY" in ''|*[!0-9]*) RETRY_DELAY=10 ;; esac
[ "$RETRY_DELAY" -le 120 ] || RETRY_DELAY=120

fail=0
for node in $NODES; do
  # The flag is prepended to the piped script rather than passed as an ssh
  # argument: the remote command stays exactly `sh -s`, so nothing downstream
  # has to parse a modified argv.
  payload="CCC_FLEET_DOCTOR=${CCC_FLEET_DOCTOR:-0}
$PROBE"
  if [ "${CCC_FLEET_DOCTOR:-0}" = "1" ]; then node_budget=90; else node_budget=30; fi
  attempt=0
  while :; do
    if [ "$node" = "$SELF" ]; then
      out=$(printf '%s' "$payload" | sh -s 2>/dev/null)
    else
      out=$(printf '%s' "$payload" | timeout "$node_budget" "$SSH_BIN" -o BatchMode=yes -o ConnectTimeout=8 "$node" sh -s 2>/dev/null)
    fi
    [ -n "$out" ] && break
    attempt=$((attempt + 1))
    [ "$attempt" -gt "$RETRIES" ] && break
    [ "$RETRY_DELAY" -gt 0 ] && sleep "$RETRY_DELAY"
  done

  if [ -z "$out" ]; then
    echo "UNREACHABLE $node"; fail=1; continue
  fi

  avail=$(printf '%s\n' "$out" | sed -n 's/^AVAIL=//p' | head -1)
  runtime=$(printf '%s\n' "$out" | sed -n 's/^RUNTIME=//p' | head -1)
  unit=$(printf '%s\n' "$out" | sed -n 's/^UNIT=//p' | head -1)

  if [ "$avail" != "yes" ]; then
    echo "DOWN $node"; fail=1; continue
  fi

  # Reported before the boot-path comparison and separately from it: a node
  # serving from a work tree is already running unreviewed code, whether or not
  # its unit agrees. Agreement on a wrong path is the worse state, not the
  # better one, because it is the state that survives a restart.
  if ! is_canonical_root "$runtime"; then
    echo "NONCANONICAL $node runtime=$runtime"; fail=1; continue
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

  # gongmyoung dual-domain coherence (#980). Emitted only by the doctor sweep;
  # "-" (single-domain node) and "skip" (probe lacked root) are not failures.
  dual=$(printf '%s\n' "$out" | sed -n 's/^DUALDOMAIN=//p' | head -1)
  case "$dual" in
    fail\ *) echo "DUALDOMAIN $node ${dual#fail }"; fail=1; continue ;;
  esac

  echo "OK $node ($runtime)"
done
exit $fail
