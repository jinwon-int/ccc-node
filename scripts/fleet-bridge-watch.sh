#!/usr/bin/env bash
# fleet-bridge-watch — periodic fleet bridge health + boot-path check.
#
# Intended as an agent-cron command payload: exits nonzero when any node fails,
# which drives telegram-owner-on-failure notification.
#
# Three checks per node:
#   1. availability — the bridge answers "Bot status: available". "degraded"
#                     (alive, but start.sh has lost the bookkeeping that makes
#                     it restartable) is reported as DEGRADED, not DOWN.
#                     A node migrated to Danso (danso #118) answers through
#                     `danso service status --json` instead; its exit code
#                     carries the same three states plus an explicit
#                     "could not determine". Such a node reports KIND=danso,
#                     a RUNTIME that is a binary path rather than a checkout
#                     root, and the GENERATION (sha256) of that binary.
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
#   CCC_FLEET_CANONICAL_DANSO_EXES space-separated glob list of binary paths a
#                    Danso node may serve from. Empty by default, which means no
#                    canonicality verdict is made for Danso nodes — see the
#                    comment beside is_canonical_danso_exe.
#   CCC_FLEET_PREPARED_ROOTS space-separated glob list of preparation roots under
#                    which an activated prepared runtime (#1527) may serve;
#                    see is_prepared_runtime
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

# Activated Termux prepared runtime (#1527, #1761). The self-update stages a
# checkout of the target sha under `<prep>/source` and its venv under
# `<prep>/job`, then hands the bridge to `start.sh --prepared-runtime <prep>/job`.
# Accepted only when all three hold: the probe reported a completed job (its
# receipt says ready), the job sits under a preparation root, and the serving
# root is that job's own `source` sibling. Patterns, never resolved paths: the
# judged paths are remote (see is_canonical_root).
PREPARED_ROOTS="${CCC_FLEET_PREPARED_ROOTS:-/data/data/com.termux/files/home/.ccc-node/preparations /home/*/.ccc-node/preparations /root/.ccc-node/preparations}"

# Binary paths a Danso node may legitimately serve from. Empty by default: the
# fleet has no agreed install path yet, and a guessed list is the stale-table
# defect described in the header. While it is empty no canonicality verdict is
# made for Danso nodes — their generation is still reported, so an operator can
# see what each node is running.
CANON_DANSO_EXES="${CCC_FLEET_CANONICAL_DANSO_EXES:-}"

is_canonical_danso_exe() {
  _exe=$1 _hit=1
  set -f
  for _pat in $CANON_DANSO_EXES; do
    # shellcheck disable=SC2254  # $_pat is a glob on purpose
    case "$_exe" in $_pat) _hit=0; break ;; esac
  done
  set +f
  return $_hit
}
is_prepared_runtime() {
  _root=$1 _job=$2 _hit=1
  [ -n "$_job" ] && [ "$_job" != "-" ] || return 1
  case "$_job" in */job) ;; *) return 1 ;; esac
  _prep=${_job%/job}
  [ "$_root" = "$_prep/source" ] || return 1
  set -f
  for _pat in $PREPARED_ROOTS; do
    # shellcheck disable=SC2254  # $_pat is a glob on purpose
    case "$_prep" in $_pat/*) _hit=0; break ;; esac
  done
  set +f
  return $_hit
}

# POSIX sh, runs on every node including Termux. Emits KEY=VALUE lines only.
read -r -d '' PROBE <<'PROBE_EOF' || true
# uid, not user: the `user` column truncates names longer than 8 characters
# ("gongmyoung" -> "gongmyo+"), and the truncated form is not a valid su target.
#
# The filter matches more than the Telegram bridge: ccc-matrix-bridge.service
# runs the same `telegram_bot` module with the same `--path`. `ps` lists by
# ascending pid, so a bare `head -1` hands whichever bridge happened to start
# first — on 2026-09-20 that was the Matrix bridge on 3 of 4 dual-bridge nodes
# (gongmyoung, gwakga, jingun), which is why gongmyoung paged
# `service-domain=unverified` every day while its Telegram bridge was healthy
# (#1860). Select on the same evidence the domain check below reads — the
# cgroup — so selection and verdict can no longer disagree.
_bridge_candidates=$(ps -eo uid=,pid=,ppid=,command= 2>/dev/null | grep 'telegram_bot' | grep -- '--path' | grep -v grep)
line=""
if [ -n "$_bridge_candidates" ]; then
  _saved_ifs=$IFS
  IFS='
'
  for _cand in $_bridge_candidates; do
    IFS=$_saved_ifs
    _cand_pid=$(printf '%s' "$_cand" | awk '{print $2}')
    case "$_cand_pid" in ''|*[!0-9]*) IFS='
'; continue ;; esac
    case "$(head -1 "/proc/$_cand_pid/cgroup" 2>/dev/null)" in
      */ccc-telegram-bridge.service) line=$_cand; break ;;
    esac
    IFS='
'
  done
  IFS=$_saved_ifs
  # No cgroup named the unit: a container, a Termux/Android node with no
  # systemd, or an unreadable /proc. Fall back to the historical first match
  # rather than paging a healthy node as DOWN.
  [ -n "$line" ] || line=$(printf '%s\n' "$_bridge_candidates" | head -1)
fi
if [ -z "$line" ]; then
  # No ccc bridge. Before calling the node down, look for a Danso resident
  # service. On a migrated node the serving process is `<exe> service run
  # --data-dir <d>`: it carries neither `telegram_bot` nor `--path`, so the
  # match above cannot see it and the node would page DOWN while answering
  # Telegram normally — the same false-DOWN class as #1761 (danso #118 4-a).
  dline=$(ps -eo uid=,command= 2>/dev/null | grep -- 'service run' | grep 'danso' | grep -v grep | head -1)
  if [ -n "$dline" ]; then
    druid=$(printf '%s' "$dline" | awk '{print $1}')
    druser=$(id -nu "$druid" 2>/dev/null || printf '%s' "$druid")
    dexe=$(printf '%s' "$dline" | sed 's/^ *[0-9][0-9]* *//' | awk '{print $1}')
    echo "KIND=danso"
    # The state root, taken from the command line that is already in hand.
    # `danso service status` resolves it from `--data-dir` or from
    # DANSO_TELEGRAM_DATA_DIR, and this probe has neither: it starts a fresh
    # process carrying the *watch's* own variables, and a unit's `Environment=`
    # reaches only the service systemd itself started. Without this the
    # status call answered about a directory that does not exist, exited 2, and
    # a serving node reported AVAIL=no — measured on yukson 2026-09-17
    # (danso #118), the same false-DOWN class as #1761.
    ddir=$(printf '%s' "$dline" | sed -n 's/.*--data-dir[ =]\{1,\}\([^ ]*\).*/\1/p' | head -1)
    # `--json`, not the text form: the text is a human rendering, while the JSON
    # also carries the runtime generation. The EXIT CODE, not the printed state,
    # is the availability signal — it is the same 0/1/2/3 contract the text's
    # first line describes, and it cannot be garbled by locale or encoding.
    if [ -n "$ddir" ]; then
      set -- service status --data-dir "$ddir" --json
    else
      # A service started without the argument resolves the directory from the
      # variables it inherited; ask the same way and let the exit code speak.
      set -- service status --json
    fi
    if [ "$(id -u)" = "$druid" ]; then
      dj=$("$dexe" "$@" 2>/dev/null); drc=$?
    elif [ "$(id -u)" != 0 ]; then
      dj=$(sudo -n -H -u "$druser" -- "$dexe" "$@" 2>/dev/null); drc=$?
    else
      if [ -n "$ddir" ]; then
        dj=$(su - "$druser" -c "'$dexe' service status --data-dir '$ddir' --json" 2>/dev/null); drc=$?
      else
        dj=$(su - "$druser" -c "'$dexe' service status --json" 2>/dev/null); drc=$?
      fi
    fi
    case "$drc" in
      0) echo "AVAIL=yes" ;;
      1) echo "AVAIL=degraded" ;;
      2) echo "AVAIL=no" ;;
      # Exit 3 is danso's explicit "could not determine", and any other code is
      # a failed inspection. Neither is evidence that the live process is down.
      *) echo "AVAIL=unverified" ;;
    esac
    # The path the service is actually running from, and the digest of that
    # image. "The unit restarted" is not evidence that a self-update replaced
    # anything — a restart that re-execs the same image reports success while
    # the generation is unchanged. The digest is the evidence.
    dpath=$(printf '%s' "$dj" | sed -n 's/.*"exe_path":"\([^"]*\)".*/\1/p' | head -1)
    dgen=$(printf '%s' "$dj" | sed -n 's/.*"binary_sha256":"\([^"]*\)".*/\1/p' | head -1)
    echo "RUNTIME=${dpath:-$dexe}"
    echo "GENERATION=${dgen:--}"
    dunit_exe=""
    for u in /etc/systemd/system/danso.service /root/.config/systemd/user/danso.service /home/*/.config/systemd/user/danso.service; do
      [ -f "$u" ] || continue
      dexec=$(grep -m1 '^ExecStart=' "$u" 2>/dev/null | sed 's/^ExecStart=//')
      [ -n "$dexec" ] || continue
      dunit_exe=$(printf '%s' "$dexec" | awk '{print $1}')
      [ -n "$dunit_exe" ] && break
    done
    echo "UNIT=${dunit_exe:--}"
    # Neither applies to a Danso node: ccc-doctor inspects a ccc checkout, and
    # the dual-domain checks are about the gongmyoung bridge account.
    echo "DOCTOR=-"
    echo "DUALDOMAIN=-"
    echo "PREPARED=-"
    echo "PROBE_COMPLETE=1"
    exit 0
  fi
  echo "RUNTIME=-"; echo "AVAIL=no"; echo "UNIT=-"; echo "PREPARED=-"; echo "PROBE_COMPLETE=1"; exit 0
fi
echo "KIND=ccc"
runuid=$(printf '%s' "$line" | awk '{print $1}')
runpid=$(printf '%s' "$line" | awk '{print $2}')
runppid=$(printf '%s' "$line" | awk '{print $3}')
runuser=$(id -nu "$runuid" 2>/dev/null || printf '%s' "$runuid")
cmd=$(printf '%s' "$line" | sed 's/^ *[0-9][0-9]* *[0-9][0-9]* *[0-9][0-9]* *//')
root=""
for tok in $cmd; do case "$tok" in */bridge/*) root=${tok%%/bridge/*}; break ;; esac; done
bpath=$(printf '%s' "$cmd" | awk '{for(i=1;i<NF;i++) if($i=="--path") {print $(i+1); exit}}')
# Prepared runtime (docs/prepared-runtime-launch.md, #1527): the worker is
# `<job>/runtime/bin/python -m telegram_bot`, so its command line carries no
# `/bridge/` token and the root cannot be read from it. The supervisor that
# spawned it names both halves — `<source>/bridge/start.sh --path <p>
# --_daemon_supervisor --prepared-runtime <job>` — so read the root and the job
# from there. On 2026-09-16 the missing root made this probe answer AVAIL=no
# for daegyo while its bridge was answering Telegram (#1761). The supervisor
# is consulted whenever it exists, so a prepared launch is reported even when
# the worker line happens to carry a /bridge/ path.
prepared=""
worker_exe=$(printf '%s' "$cmd" | awk '{print $1}')
# The source must come from THIS worker's parent, not the first supervisor on
# the host. Bind owner, project, and selected interpreter before using it.
sup=$(ps -eo uid=,pid=,command= 2>/dev/null | awk -v uid="$runuid" -v pid="$runppid" '$1 == uid && $2 == pid {print; exit}')
if [ -n "$sup" ]; then
  supcmd=$(printf '%s' "$sup" | sed 's/^ *[0-9][0-9]* *[0-9][0-9]* *//')
  case " $supcmd " in
    *' --_daemon_supervisor '*)
      candidate=$(printf '%s' "$supcmd" | awk '{for(i=1;i<NF;i++) if($i=="--prepared-runtime") {print $(i+1); exit}}')
      sup_path=$(printf '%s' "$supcmd" | awk '{for(i=1;i<NF;i++) if($i=="--path") {print $(i+1); exit}}')
      if [ -n "$candidate" ] && [ "$sup_path" = "$bpath" ] && [ "$worker_exe" = "$candidate/runtime/bin/python" ]; then
        prepared=$candidate
        if [ -z "$root" ]; then
          for tok in $supcmd; do case "$tok" in */bridge/start.sh) root=${tok%/bridge/start.sh}; break ;; esac; done
        fi
      fi ;;
  esac
fi
# A visible worker with an unrecognized layout is a failed inspection, not
# evidence of downtime. Keep absence and confirmed unavailable as AVAIL=no.
[ -n "$root" ] || { echo "RUNTIME=-"; echo "AVAIL=unverified"; echo "UNIT=-"; echo "PREPARED=-"; echo "PROBE_COMPLETE=1"; exit 0; }
echo "RUNTIME=$root"
# The job is reported only when its receipt says the preparation completed;
# an unfinished or absent receipt leaves the launch to the canonical-root check.
if [ -n "$prepared" ] && grep -q '"status": *"ready"' "$prepared/receipt.json" 2>/dev/null; then
  echo "PREPARED=$prepared"
else
  echo "PREPARED=-"
fi

# Metadata code travels with the watcher: peers need not upgrade or install it.
# No temporary remote files, receipt writes, or launches are performed.
metadata() {
  python3 - "$1" "$root" "$prepared" "$bpath" "$runuid" <<'METADATA_PY'
__CCC_FLEET_METADATA_SOURCE__
METADATA_PY
}
case "$root" in
  */.ccc-node/checkouts/*)
    if [ -n "$prepared" ] && [ "$(metadata checkout 2>/dev/null)" = verified ]; then
      echo 'CHECKOUT=verified'
    else
      echo 'CHECKOUT=unverified'
    fi ;;
esac

# availability — run start.sh as the account that owns the process.
# Name the interpreter (the #1160 defect class): shebang-exec'ing start.sh
# fails on Termux where /bin/bash does not exist outside termux-exec
# contexts, and the empty $st then paged a false "DOWN <node>". The su
# fallback also used '~' inside single quotes, which never expands — use
# $HOME resolved by the target account's login shell.
st=""
if [ "$(id -u)" = "$runuid" ]; then
  st=$(bash "$root/bridge/start.sh" --path "${bpath:-$HOME}" --status 2>/dev/null)
elif [ "$(id -u)" != 0 ]; then
  # SSH may land on the unprivileged host account while Danso/CCC runs as root.
  # Use only an already-authorized, non-interactive sudo path; never prompt.
  st=$(sudo -n -H -u "$runuser" -- bash "$root/bridge/start.sh" --path "${bpath:-$HOME}" --status 2>/dev/null)
else
  if [ -n "$bpath" ]; then
    st=$(su - "$runuser" -c "bash '$root/bridge/start.sh' --path '$bpath' --status" 2>/dev/null)
  else
    st=$(su - "$runuser" -c "bash '$root/bridge/start.sh' --path \"\$HOME\" --status" 2>/dev/null)
  fi
fi
# Three states, not two. `degraded` means the process is alive and serving but
# start.sh has lost its bookkeeping (typically a missing pid file), so --stop
# and --restart cannot recover it. Collapsing that into AVAIL=no made the sweep
# page "DOWN gongyung" on 2026-08-11 for a bridge that was answering Telegram
# normally — the same cry-wolf failure the retry logic below was added to end.
if printf '%s' "$st" | grep -q 'Bot status: available'; then
  echo "AVAIL=yes"
elif printf '%s' "$st" | grep -q 'Bot status: degraded'; then
  echo "AVAIL=degraded"
elif ! printf '%s' "$st" | grep -q 'Bot status: unavailable'; then
  # A failed inspection is not evidence that the live process is down.
  echo "AVAIL=unverified"
else
  echo "AVAIL=no"
fi

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
#
# Known false positive, deliberately NOT worked around here: on piri nodes the
# doctor reports `distill extractor ... executable=missing` for a CLI that is
# installed and healthy, because CCC_PIRI_CLI_PATH exists only as a systemd
# `Environment=` line on the bridge unit and a `su -` login shell never sees it.
# Carrying the bridge's CLI-path variables across looks like the obvious fix and
# is not: CCC_CODEX_CLI_PATH points at the memory-materializing ccc-codex
# wrapper, and the doctor's Codex `--version` probe times out through it, so the
# carry trades four false DRIFTs on piri nodes for a false DRIFT on every codex
# node (measured on dungae and daegyo, 2026-08-11). The fix belongs in the
# doctor, which alone knows which of its checks are existence tests and which
# are live probes.
if [ "${CCC_FLEET_DOCTOR:-0}" = "1" ]; then
  cdir="${bpath:-$HOME}/.claude"
  doctor_root=$root
  # A staged runtime is not necessarily where setup installed the harness.
  # Require its operator-owned install reference; never search for a checkout
  # that happens to pass. Missing/unsafe references remain an explicit alert.
  if [ -n "$prepared" ]; then
    doctor_root=$(metadata installed 2>/dev/null) || doctor_root=""
  fi
  if [ -z "$doctor_root" ] || [ ! -f "$doctor_root/scripts/ccc-doctor.sh" ]; then
    echo 'DOCTOR=unverified'
  else
    echo "DOCTOR_ROOT=$doctor_root"
    if [ "$(id -u)" = "$runuid" ]; then
      CCC_DOCTOR_CLAUDE_DIR="$cdir" timeout 60 bash "$doctor_root/scripts/ccc-doctor.sh" >/dev/null 2>&1
    elif [ "$(id -u)" != 0 ]; then
      sudo -n -H -u "$runuser" -- env CCC_DOCTOR_CLAUDE_DIR="$cdir" timeout 60 bash "$doctor_root/scripts/ccc-doctor.sh" >/dev/null 2>&1
    else
      su - "$runuser" -c "CCC_DOCTOR_CLAUDE_DIR='$cdir' timeout 60 bash '$doctor_root/scripts/ccc-doctor.sh'" >/dev/null 2>&1
    fi
    echo "DOCTOR=$?"
  fi
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
# Process ownership does not select the systemd manager. User=gongmyoung
# can serve in a system unit; identify the selected worker's actual cgroup.
# An unknown domain remains an explicit failed inspection, never a silent pass.
if [ "${CCC_FLEET_DOCTOR:-0}" = "1" ] && [ "$runuser" = gongmyoung ] && id gongmyoung >/dev/null 2>&1 && [ -d /home/gongmyoung ]; then
  if [ "$(id -u)" != 0 ]; then
    # crontab/loginctl inspection needs root; say so instead of guessing.
    echo "DUALDOMAIN=skip(non-root)"
  else
    dd_fail=""
    dd_add() { dd_fail="${dd_fail:+${dd_fail},}$1"; }

    # Select the manager from the worker, not an inactive leftover unit file.
    dd_domain=unknown
    case "$runpid" in
      ''|*[!0-9]*) ;;
      *)
        if grep -q ':/system.slice/ccc-telegram-bridge\.service$' "/proc/$runpid/cgroup" 2>/dev/null; then
          dd_domain=system
        elif grep -q ':/user.slice/.*\/ccc-telegram-bridge\.service$' "/proc/$runpid/cgroup" 2>/dev/null; then
          dd_domain=user
        fi ;;
    esac

    # 1. Both layouts need the updater, but only a user unit needs a user bus.
    gcron=$(crontab -u gongmyoung -l 2>/dev/null || true)
    printf '%s\n' "$gcron" | grep -q 'ccc-self-update' || dd_add 'cron-self-update-missing'
    if [ "$dd_domain" = user ]; then
      printf '%s\n' "$gcron" | grep -q 'XDG_RUNTIME_DIR' \
        && printf '%s\n' "$gcron" | grep -q 'DBUS_SESSION_BUS_ADDRESS' \
        || dd_add 'cron-bus-env-missing'
    fi

    # 2. Check access as the updater account. Root-owned immutable objects and
    # refs can be usable through directory permissions/ACLs. Conversely, a
    # matching owner does not guarantee usable permissions. Do not write a
    # canary, create an index lock, or interpret a failed git command as clean.
    repo=/opt/ccc-node
    [ -d "$repo/.git" ] || dd_add 'repo-missing'
    [ "$(stat -c %U "$repo" 2>/dev/null || echo ?)" = "gongmyoung" ] || dd_add 'repo-not-gongmyoung-owned'
    if dd_status=$(su - gongmyoung -c "git -C $repo status --porcelain" 2>/dev/null); then
      [ -z "$dd_status" ] || dd_add 'repo-dirty'
    else
      dd_add 'repo-status-unverified'
    fi
    dd_branch=$(su - gongmyoung -c "git -C $repo rev-parse --abbrev-ref HEAD" 2>/dev/null || echo ?)
    [ "$dd_branch" = "main" ] || dd_add "repo-branch=$dd_branch"
    # Atomic ref/index replacement needs writable directories; existing object
    # files only need reads. Reflogs and FETCH_HEAD are opened for in-place
    # writes, so those existing files also need write permission.
    if dd_access=$(su - gongmyoung -c "cd '$repo' && find .git \( ! -readable -o \( -type d ! -writable \) -o \( \( -path '.git/logs/*' -o -path '.git/FETCH_HEAD' \) -type f ! -writable \) \) -print -quit" 2>/dev/null); then
      [ -z "$dd_access" ] || dd_add 'git-access-denied'
    else
      dd_add 'git-access-unverified'
    fi

    # 3. Require the manager that actually owns the worker. A stale user unit
    # must not page an active system service; neither may unknown ownership pass.
    if [ "$dd_domain" = system ]; then
      dd_unit=$(systemctl is-active ccc-telegram-bridge.service 2>/dev/null || true)
      [ "$dd_unit" = active ] || dd_add "system-unit=${dd_unit:-unknown}"
      dd_owner=$(systemctl show ccc-telegram-bridge.service -p User --value 2>/dev/null || true)
      dd_owner_uid=""
      [ -z "$dd_owner" ] || dd_owner_uid=$(id -u "$dd_owner" 2>/dev/null || true)
      [ "$dd_owner_uid" = "$runuid" ] || dd_add "system-unit-owner=${dd_owner:-unknown}"
    elif [ "$dd_domain" = user ]; then
      uid_g=$runuid
      dd_bus="XDG_RUNTIME_DIR=/run/user/$uid_g DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$uid_g/bus"
      dd_unit=$(su - gongmyoung -c "env $dd_bus systemctl --user is-active ccc-telegram-bridge" 2>/dev/null || true)
      [ "$dd_unit" = "active" ] || dd_add "user-unit=${dd_unit:-unknown}"
      dd_mgr=$(systemctl is-active "user@$uid_g" 2>/dev/null || true)
      [ "$dd_mgr" = "active" ] || dd_add "user-manager=${dd_mgr:-unknown}"
      dd_linger=$(loginctl show-user gongmyoung 2>/dev/null | sed -n 's/^Linger=//p' | head -1)
      [ "$dd_linger" = "yes" ] || dd_add 'linger=unknown-or-disabled'
    else
      dd_add 'service-domain=unverified'
    fi

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
echo "PROBE_COMPLETE=1"
PROBE_EOF
META_FILE="$(cd "$(dirname "$0")" && pwd)/fleet_watch_metadata.py"
[ -r "$META_FILE" ] || { echo 'UNVERIFIED watcher metadata-source=missing'; exit 1; }
META_SOURCE=$(cat "$META_FILE")
PROBE="${PROBE%%__CCC_FLEET_METADATA_SOURCE__*}$META_SOURCE${PROBE#*__CCC_FLEET_METADATA_SOURCE__}"
# Read-only seam used by tests to execute the exact transmitted probe.
if [ "${1:-}" = --print-probe ]; then printf '%s\n' "$PROBE"; exit 0; fi

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
    probe_rc=$?
    [ -n "$out" ] && break
    attempt=$((attempt + 1))
    [ "$attempt" -gt "$RETRIES" ] && break
    [ "$RETRY_DELAY" -gt 0 ] && sleep "$RETRY_DELAY"
  done

  if [ -z "$out" ]; then
    echo "UNREACHABLE $node"; fail=1; continue
  fi

  # Partial stdout does not prove the inspection finished. The probe is sent
  # by this watcher, so there is no older remote protocol to fall back to.
  if [ "$probe_rc" != 0 ] || [ "$(printf '%s\n' "$out" | tail -1)" != PROBE_COMPLETE=1 ]; then
    echo "UNVERIFIED $node inspection=incomplete-probe"; fail=1; continue
  fi

  avail=$(printf '%s\n' "$out" | sed -n 's/^AVAIL=//p' | head -1)
  runtime=$(printf '%s\n' "$out" | sed -n 's/^RUNTIME=//p' | head -1)
  # CCC is the default; Danso explicitly emits its kind.
  kind=$(printf '%s\n' "$out" | sed -n 's/^KIND=//p' | head -1)
  [ -n "$kind" ] || kind=ccc
  generation=$(printf '%s\n' "$out" | sed -n 's/^GENERATION=//p' | head -1)
  unit=$(printf '%s\n' "$out" | sed -n 's/^UNIT=//p' | head -1)

  # DEGRADED is still a failure — an unmanaged bridge cannot be restarted by
  # start.sh, so the next self-update or recovery attempt on that node has
  # nothing to act on. It is reported under its own name because the operator
  # response differs: DOWN means restore service, DEGRADED means restore
  # bookkeeping for a service that is already answering.
  if [ "$avail" = "degraded" ]; then
    echo "DEGRADED $node runtime=$runtime"; fail=1; continue
  fi

  if [ "$avail" != yes ] && [ "$avail" != no ]; then
    echo "UNVERIFIED $node runtime=$runtime"; fail=1; continue
  fi
  if [ "$avail" != "yes" ]; then
    echo "DOWN $node"; fail=1; continue
  fi

  # Reported before the boot-path comparison and separately from it: a node
  # serving from a work tree is already running unreviewed code, whether or not
  # its unit agrees. Agreement on a wrong path is the worse state, not the
  # better one, because it is the state that survives a restart.
  #
  # A prepared runtime is the one sanctioned exception: the Termux self-update
  # activates `<prep>/source` with the venv in `<prep>/job` (#1527), and that
  # source is a fresh checkout of the target sha, not a work tree. It is
  # accepted only when the supervisor named a completed job AND the serving
  # root is that job's own source sibling — a job pointing at some other
  # checkout is exactly the shape the check exists to catch. The launch stays
  # visible in the OK line so an operator can tell it from a plain checkout.
  #
  # A Danso node's runtime is a BINARY path, not a checkout root, so the
  # ccc-node glob list cannot judge it. Applying it anyway would report
  # NONCANONICAL for every healthy Danso node — the cry-wolf failure the rest
  # of this file exists to prevent.
  #
  # No substitute glob list is invented here. The equivalent question for a
  # binary is "is this the generation we published?", and that answer belongs
  # to the signed-release model (danso #119), not to a hardcoded path table —
  # the "hardcoded table goes stale silently" defect named in the header. Until
  # that exists, an operator may opt in with an explicit allowlist; the default
  # is empty, so the check does not run rather than guessing an answer.
  prepared=$(printf '%s\n' "$out" | sed -n 's/^PREPARED=//p' | head -1)
  prepared_tag=""
  checkout=$(printf '%s\n' "$out" | sed -n 's/^CHECKOUT=//p' | head -1)
  if [ "$kind" = danso ]; then
    if [ -n "$CANON_DANSO_EXES" ] && ! is_canonical_danso_exe "$runtime"; then
      echo "NONCANONICAL $node runtime=$runtime"; fail=1; continue
    fi
  elif ! is_canonical_root "$runtime"; then
    if [ "$checkout" = verified ]; then
      prepared_tag=", verified-checkout:${prepared##*/}"
    elif [ "$checkout" = unverified ]; then
      echo "UNVERIFIED $node runtime=$runtime inspection=prepared-checkout"; fail=1; continue
    elif is_prepared_runtime "$runtime" "$prepared"; then
      prep_dir=${prepared%/*}
      prepared_tag=", prepared:${prep_dir##*/}"
    else
      echo "NONCANONICAL $node runtime=$runtime"; fail=1; continue
    fi
  fi

  # A unit pointing elsewhere is silent while the bridge is up: the next reboot
  # serves the stale checkout. Report it even though availability passed.
  if [ -n "$unit" ] && [ "$unit" != "-" ] && [ "$unit" != "$runtime" ]; then
    echo "BOOTPATH $node unit=$unit runtime=$runtime"; fail=1; continue
  fi

  doctor=$(printf '%s\n' "$out" | sed -n 's/^DOCTOR=//p' | head -1)
  if [ "$doctor" = unverified ]; then
    echo "UNVERIFIED $node runtime=$runtime inspection=harness-reference"; fail=1; continue
  fi
  dual=$(printf '%s\n' "$out" | sed -n 's/^DUALDOMAIN=//p' | head -1)
  if [ "${CCC_FLEET_DOCTOR:-0}" = 1 ] && { [ -z "$doctor" ] || [ -z "$dual" ] || { [ "$kind" = ccc ] && [ "$doctor" = - ]; }; }; then
    echo "UNVERIFIED $node runtime=$runtime inspection=incomplete-doctor"; fail=1; continue
  fi
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

  # The generation is shown for Danso nodes because nothing else identifies
  # which image is serving: two nodes at the same path can be running different
  # binaries, and a restart that re-execs the same image looks identical to one
  # that replaced it. Short form only — the full digest is in the probe output.
  gen_tag=""
  if [ "$kind" = danso ] && [ -n "$generation" ] && [ "$generation" != "-" ]; then
    gen_tag=", generation:$(printf '%s' "$generation" | cut -c1-12)"
  fi
  echo "OK $node ($runtime$prepared_tag$gen_tag)"
done
exit $fail
