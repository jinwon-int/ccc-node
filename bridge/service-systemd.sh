#!/bin/bash

# ccc-node Telegram bridge — Linux systemd startup-service subcommand.
#
# Extracted verbatim from bridge/start.sh systemd_paths/do_install_systemd/
# do_uninstall_systemd (#584 P3-2) so the service-install machinery is
# testable in isolation. start.sh --install-systemd/--uninstall-systemd
# dispatches here after its own pre-flight guards (check_env); this script
# only generates/removes the unit and drives systemctl. The generated unit is
# byte-identical to what start.sh produced before the extraction.
#
# Runs the bridge in the FOREGROUND under systemd supervision (Type=simple):
# start.sh's own prepare_runtime + exec_bot_once handle venv/deps/token-lock,
# and systemd handles restart-on-crash and reboot persistence — so we
# deliberately do NOT pass -d/--daemon in ExecStart. Service unit name is
# overridable via BRIDGE_SERVICE_NAME (default ccc-telegram-bridge), letting
# one host run multiple bridges (e.g. ccc-telegram-bridge-<slug>). Installs a
# system unit when run as root, otherwise a `systemctl --user` unit under
# ~/.config/systemd/user.
#
# Usage:
#   service-systemd.sh install   --project-root <dir> [--proxy-url <url>] [--caller <name>]
#   service-systemd.sh reconcile [--dry-run]
#   service-systemd.sh memory-guard [--dry-run] [--remove]
#   service-systemd.sh uninstall [--caller <name>]
#
# Memory guard (#1877): install also writes <unit>.d/10-ccc-memory-guard.conf
# with MemoryHigh=/MemoryMax= (default 50% / 75% of host RAM) and
# OOMPolicy=continue, so one runaway child is throttled/killed inside the
# bridge cgroup instead of taking the whole unit (and every in-flight session)
# down with it. `memory-guard` writes the same drop-in for an ALREADY installed
# unit (any BRIDGE_SERVICE_NAME, e.g. ccc-matrix-bridge) and daemon-reloads
# without restarting. daemon-reload alone leaves a RUNNING unit's cgroup at
# memory.high/max=max until its next start (#2545), so when the unit is active
# memory-guard also applies the same limits with `systemctl set-property
# --runtime` (percentages resolved to bytes from MemTotal) and prints the
# effective /sys/fs/cgroup/<ControlGroup>/memory.{high,max}; a cgroup still at
# `max` is reported as NOT applied and exits 1. `memory-guard --remove` is the
# rollback: it removes the generated drop-in, daemon-reloads and, on an active
# unit, clears the runtime limits (MemoryHigh=infinity MemoryMax=infinity).
# reconcile never touches it. Hosts without systemd (Termux) never reach this
# script's unit paths.
#   CCC_BRIDGE_MEMORY_GUARD=0  install: skip the drop-in
#   CCC_BRIDGE_MEMORY_HIGH     MemoryHigh= value (default: the existing
#                              ccc-generated drop-in's value, else 50%)
#   CCC_BRIDGE_MEMORY_MAX      MemoryMax= value (default: the existing
#                              ccc-generated drop-in's value, else 75%)
#   Unset values are kept from that drop-in on install and memory-guard reruns
#   (#2633), so per-node limits survive; pass 50%/75% explicitly to reset.
#   Values: 1%..100%, bytes with optional K/M/G/T suffix, or infinity.
#   The 50%/75% defaults suit large hosts; on a ~3 GB node 50% (~1.5 GB) can
#   throttle normal sessions, so set explicit per-node values there first.
#
# Inputs (resolved by start.sh when dispatched from there):
#   --project-root  absolute project path (was $PROJECT_ROOT); install only
#   --proxy-url     PROXY_URL resolved via read_env_with_fallback; empty = none
#   --caller        script name to print in user-facing hints (default:
#                   <this dir>/start.sh)
#   BRIDGE_SERVICE_NAME  env: unit base name (default: ccc-telegram-bridge)
#
# Test seams (same pattern as scripts/install-agent-cron-systemd.sh; defaults
# preserve production behavior):
#   CCC_SYSTEMD_DIR    unit directory override (default: scope-derived)
#   CCC_SYSTEMCTL      systemctl command override (default: systemctl)
#   CCC_SYSTEMD_SCOPE  scope override accepted only with CCC_SYSTEMD_DIR
#                      (system|user; tests only)
#   CCC_CGROUP_ROOT    cgroup v2 mount for memory-guard verification
#                      (default: /sys/fs/cgroup)
#   CCC_MEMINFO_FILE   MemTotal source for percent limits (default: /proc/meminfo)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SYSTEMCTL_BIN="${CCC_SYSTEMCTL:-systemctl}"
CGROUP_ROOT="${CCC_CGROUP_ROOT:-/sys/fs/cgroup}"
MEMINFO_FILE="${CCC_MEMINFO_FILE:-/proc/meminfo}"

usage() {
    cat <<EOF
Usage: $0 <install|reconcile|memory-guard|uninstall|is-managed|main-pid> [options]

Subcommands:
  install     Generate the systemd unit and enable --now it
  reconcile   If an existing ccc-generated unit has drifted, atomically replace
              its canonical main unit and daemon-reload without changing its
              enabled/active state or restarting the bridge
  memory-guard
              Write the bridge memory-guard drop-in (MemoryHigh/MemoryMax,
              OOMPolicy=continue) for an installed unit and daemon-reload
              without restarting it (#1877); an active unit also gets the
              limits via set-property --runtime and the effective cgroup
              memory.high/max are verified (#2545). With --remove: delete the
              generated drop-in and clear the runtime limits (rollback)
  uninstall   Disable the service and remove the unit
  is-managed  Exit 0 iff the bridge unit file exists and systemctl reports it
              active (used by start.sh --restart to avoid supervisor fights)
  main-pid    Print the MainPID of the active managed bridge unit (same
              ownership rule as is-managed); exit 1 with no output otherwise
              (used by start.sh --status to reconcile a service-managed bot
              whose pid file was lost to the concurrent-instance race)

Options:
  --project-root <dir>  Project root directory (required for install)
  --proxy-url <url>     Proxy URL to embed in the unit environment
  --caller <name>       Script name shown in usage hints
  --dry-run             Report reconcile/memory-guard actions without writing
                        or systemctl
  --remove              memory-guard only: roll the guard back instead
  --allow-relocate      Let reconcile move the unit onto THIS checkout even when
                        the installed unit points at a different one. Off by
                        default: relocation is a deliberate act, not drift.
  -h, --help            Show this help message and exit
EOF
}

SUBCOMMAND=""
PROJECT_ROOT_ARG=""
PROXY_URL_ARG=""
CALLER=""
DRY_RUN=0
ALLOW_RELOCATE=0
REMOVE=0

while [ $# -gt 0 ]; do
    case "$1" in
        install|reconcile|memory-guard|uninstall|is-managed|main-pid)
            SUBCOMMAND="$1"
            shift
            ;;
        --project-root)
            [ "$#" -ge 2 ] || { echo "--project-root requires a value" >&2; exit 2; }
            PROJECT_ROOT_ARG="$2"
            shift 2
            ;;
        --proxy-url)
            [ "$#" -ge 2 ] || { echo "--proxy-url requires a value" >&2; exit 2; }
            PROXY_URL_ARG="$2"
            shift 2
            ;;
        --caller)
            [ "$#" -ge 2 ] || { echo "--caller requires a value" >&2; exit 2; }
            CALLER="$2"
            shift 2
            ;;
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        --allow-relocate)
            ALLOW_RELOCATE=1
            shift
            ;;
        --remove)
            REMOVE=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "❌ Error: Unknown option: $1"
            usage >&2
            exit 1
            ;;
    esac
done

if [ -z "$SUBCOMMAND" ]; then
    usage >&2
    exit 1
fi
if [ "$DRY_RUN" = "1" ] && [ "$SUBCOMMAND" != "reconcile" ] && [ "$SUBCOMMAND" != "memory-guard" ]; then
    echo "❌ Error: --dry-run is supported only with reconcile and memory-guard" >&2
    exit 1
fi
if [ "$REMOVE" = "1" ] && [ "$SUBCOMMAND" != "memory-guard" ]; then
    echo "❌ Error: --remove is supported only with memory-guard" >&2
    exit 1
fi
if [ "$ALLOW_RELOCATE" = "1" ] && [ "$SUBCOMMAND" != "reconcile" ]; then
    echo "❌ Error: --allow-relocate is supported only with reconcile" >&2
    exit 1
fi
CALLER="${CALLER:-$SCRIPT_DIR/start.sh}"

systemd_paths() {
    # Sets SYSTEMD_UNIT_FILE, SYSTEMCTL (array), SYSTEMD_SCOPE based on euid.
    SYSTEMD_SERVICE="${BRIDGE_SERVICE_NAME:-ccc-telegram-bridge}.service"
    if [ "$(id -u)" = "0" ]; then
        SYSTEMD_SCOPE="system"
        SYSTEMD_UNIT_DIR="/etc/systemd/system"
        SYSTEMCTL=("$SYSTEMCTL_BIN")
    else
        SYSTEMD_SCOPE="user"
        SYSTEMD_UNIT_DIR="$HOME/.config/systemd/user"
        SYSTEMCTL=("$SYSTEMCTL_BIN" --user)
    fi
    # Test seam: never changes production paths unless explicitly exported.
    if [ -n "${CCC_SYSTEMD_DIR:-}" ]; then
        SYSTEMD_UNIT_DIR="$CCC_SYSTEMD_DIR"
        case "${CCC_SYSTEMD_SCOPE:-}" in
            system)
                SYSTEMD_SCOPE="system"
                SYSTEMCTL=("$SYSTEMCTL_BIN")
                ;;
            user)
                SYSTEMD_SCOPE="user"
                SYSTEMCTL=("$SYSTEMCTL_BIN" --user)
                ;;
            "") ;;
            *)
                echo "❌ Error: CCC_SYSTEMD_SCOPE must be system or user" >&2
                exit 1
                ;;
        esac
    fi
    SYSTEMD_UNIT_FILE="$SYSTEMD_UNIT_DIR/$SYSTEMD_SERVICE"
}

validate_render_home() {
    # Environment=HOME baked into the unit must be a durable login home. A
    # scratch HOME leaked from a root test run rewrote a live unit with
    # HOME=$TMP/wk-home and sent that node's session transcripts and memory
    # hooks to /tmp (#885), so the unit-writing subcommands fail closed here.
    # Hermetic tests route through the CCC_SYSTEMD_DIR seam and are exempt.
    [ -n "${CCC_SYSTEMD_DIR:-}" ] && return 0
    local service_name="${BRIDGE_SERVICE_NAME:-ccc-telegram-bridge}.service"
    if [ -z "${HOME:-}" ]; then
        # Headless contexts (transient units, timers) legitimately lack HOME;
        # mirror setup.sh and derive it from the passwd database.
        HOME="$(getent passwd "$(id -u)" 2>/dev/null | cut -d: -f6 || true)"
        [ -n "$HOME" ] || HOME=/root
        export HOME
        echo "⚠️  HOME was unset; derived HOME=$HOME from the passwd database"
    fi
    local tmp_root="${TMPDIR:-/tmp}"
    case "$HOME" in
        "$tmp_root"|"$tmp_root"/*|/tmp|/tmp/*|/var/tmp|/var/tmp/*|/dev/shm|/dev/shm/*)
            echo "❌ Error: refusing to bake ephemeral HOME=$HOME into $service_name (#885)" >&2
            echo "   Unit Environment=HOME must be a durable login home. Test runs must set" >&2
            echo "   CCC_SYSTEMD_DIR to route away from the live systemd tree." >&2
            exit 1
            ;;
    esac
    if [ ! -d "$HOME" ]; then
        echo "❌ Error: HOME=$HOME does not exist; refusing to render $service_name (#885)" >&2
        exit 1
    fi
}

bridge_env_file_path() {
    # Canonical shared bridge EnvironmentFile (#1771). Read by the Telegram unit
    # rendered here AND by the Matrix unit (service-systemd-matrix.service.example),
    # so provider/auth keys (CCC_AGENT_PROVIDER, CCC_PIRI_*, CLAUDE_CODE_OAUTH_TOKEN
    # ...) live in one place instead of hand-mirrored drop-ins. Secrets go in
    # this 0600 file only — never in an Environment= line (world-readable unit).
    printf '%s\n' "${HOME}/.config/ccc-node/bridge.env"
}

render_systemd_unit() {
    local project_root="$1" proxy_url="$2"
    local project_slug svc_path proxy_env="" wanted_by="default.target"
    project_slug="$(basename "$project_root" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9' '-' | sed 's/-*$//')"

    # Build PATH so the claude CLI (often in ~/.local/bin) is reachable from the unit.
    svc_path="$PATH"
    if [ -d "$HOME/.local/bin" ] && ! echo "$svc_path" | grep -q "$HOME/.local/bin"; then
        svc_path="$HOME/.local/bin:$svc_path"
    fi
    if [ -n "$proxy_url" ]; then
        proxy_env="Environment=http_proxy=${proxy_url}
Environment=https_proxy=${proxy_url}
Environment=all_proxy=${proxy_url}
Environment=no_proxy=localhost,127.0.0.1,192.168.0.0/16,10.0.0.0/8,172.16.0.0/12"
    fi
    [ "$SYSTEMD_SCOPE" = "system" ] && wanted_by="multi-user.target"

    cat <<UNIT | sed '/^$/d'
[Unit]
Description=ccc-node Telegram bridge (${project_slug})
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
WorkingDirectory=${REPO_ROOT}
Environment=HOME=${HOME}
Environment=PATH=${svc_path}
${proxy_env}
# Shared provider/auth env for BOTH bridge units (Telegram + Matrix), owner-only
# 0600 (#1771). Optional ("-"): a node without the file keeps its old env.
EnvironmentFile=-$(bridge_env_file_path)
ExecStart=/bin/bash ${SCRIPT_DIR}/start.sh --path ${project_root}
# Recover when the bridge handles a direct SIGTERM as a clean exit. An explicit
# systemctl stop still suppresses restart, preserving operator stop semantics.
Restart=always
RestartSec=3
# SIGTERM only the bridge main process while it closes turn admission and drains
# tracked provider/background work. At the bounded timeout systemd SIGKILLs the
# entire cgroup, so descendants cannot escape and accumulate as orphans (#303).
KillMode=mixed
SendSIGKILL=yes
TimeoutStopSec=70
[Install]
WantedBy=${wanted_by}
UNIT
}

rendered_unit_value() {
    # Command substitution strips trailing newlines. A sentinel preserves the
    # renderer's exact bytes so dry-run comparison needs no temporary file.
    local rendered
    rendered="$(render_systemd_unit "$1" "$2"; printf x)"
    printf '%s' "${rendered%x}"
}

# ── Memory guard drop-in (#1877) ────────────────────────────────────────────
# One agent-launched child growing to ~27 GB on a 32 GB host was OOM-killed by
# the kernel; with the default OOMPolicy=stop systemd then SIGKILLed the whole
# bridge unit — every in-flight provider session and MCP server with it. The
# guard bounds the bridge cgroup below the host (MemoryHigh throttles and
# reclaims first, MemoryMax is the hard cgroup OOM line) and keeps the unit
# alive when one process inside it is OOM-killed, so the bridge reports that
# turn as failed instead of restarting. The file sorts first so any later
# operator drop-in (override.conf, zz-*.conf) still wins.
MEMORY_GUARD_DROPIN_NAME="10-ccc-memory-guard.conf"
MEMORY_GUARD_MARKER="# ccc-node bridge memory guard (#1877) - generated by bridge/service-systemd.sh; rerun it to change"
MEMORY_GUARD_DEFAULT_HIGH="50%"
MEMORY_GUARD_DEFAULT_MAX="75%"

memory_limit_kind() {
    # Prints percent|bytes|infinity for a valid systemd memory limit value.
    local value="$1"
    if [ "$value" = "infinity" ]; then
        echo infinity
    elif [[ "$value" =~ ^([1-9][0-9]?|100)%$ ]]; then
        echo percent
    elif [[ "$value" =~ ^[1-9][0-9]{0,15}[KMGT]?$ ]]; then
        echo bytes
    else
        return 1
    fi
}

# awk helper shared by the byte comparisons/conversions below (awk doubles:
# no shell integer overflow; exact up to 2^53 bytes).
MEMORY_BYTES_AWK='function bytes(v,  n, u) {
    n = v; u = substr(v, length(v), 1)
    if (u ~ /[KMGT]/) n = substr(v, 1, length(v) - 1); else u = ""
    n += 0
    if (u == "K") n *= 1024
    else if (u == "M") n *= 1048576
    else if (u == "G") n *= 1073741824
    else if (u == "T") n *= 1099511627776
    return n
}'

memory_limit_not_above() {
    # True when absolute limit $1 <= $2.
    awk -v a="$1" -v b="$2" "$MEMORY_BYTES_AWK"' BEGIN { exit !(bytes(a) <= bytes(b)) }'
}

host_memtotal_bytes() {
    # MemTotal in bytes — the same quantity systemd scales percent limits by.
    local kb
    kb="$(awk '$1 == "MemTotal:" { print $2; exit }' "$MEMINFO_FILE" 2>/dev/null)"
    [[ "$kb" =~ ^[1-9][0-9]*$ ]] || return 1
    awk -v kb="$kb" 'BEGIN { printf "%.0f\n", kb * 1024 }'
}

memory_limit_runtime_value() {
    # Resolve one validated limit to the value handed to set-property --runtime:
    # bytes (percent of MemTotal, page-aligned down) or infinity.
    #
    # Why not read back `systemctl show -p MemoryHigh -p MemoryMax` after the
    # reload: show reports the MERGED config, and every earlier set-property
    # --runtime left /run/systemd/system.control/<unit>.d/50-Memory*.conf,
    # which sorts after 10-ccc-memory-guard.conf. On a rerun with new
    # CCC_BRIDGE_MEMORY_* values show would echo the stale runtime limit back,
    # so the values just written are resolved here instead.
    local value="$1" kind total
    kind="$(memory_limit_kind "$value")" || return 1
    case "$kind" in
        infinity) echo infinity ;;
        bytes) awk -v v="$value" "$MEMORY_BYTES_AWK"' BEGIN { printf "%.0f\n", bytes(v) }' ;;
        percent)
            total="$(host_memtotal_bytes)" || return 1
            awk -v t="$total" -v p="${value%\%}" \
                'BEGIN { b = t * p / 100; b -= b % 4096; printf "%.0f\n", b }'
            ;;
    esac
}

memory_guard_existing_value() {
    # Prints the MemoryHigh=/MemoryMax= value ($1) of the ccc-generated drop-in
    # already on disk, or returns 1 when there is none to keep (#2633). Needs
    # systemd_paths first. A hand-written, symlinked or marker-less file is not
    # ours to read; an unparseable value is reported and not carried over.
    local key="$1" dropin value
    [ -n "${SYSTEMD_UNIT_FILE:-}" ] || return 1
    dropin="$SYSTEMD_UNIT_FILE.d/$MEMORY_GUARD_DROPIN_NAME"
    [ -f "$dropin" ] && [ ! -L "$dropin" ] || return 1
    [ "$(head -n 1 "$dropin" 2>/dev/null)" = "$MEMORY_GUARD_MARKER" ] || return 1
    value="$(sed -n "s/^${key}=//p" "$dropin" | tail -n 1)"
    [ -n "$value" ] || return 1
    if ! memory_limit_kind "$value" >/dev/null; then
        echo "⚠️  ignoring invalid $key=$value in $dropin; using the default" >&2
        return 1
    fi
    printf '%s\n' "$value"
}

resolve_memory_guard() {
    # Sets MEMORY_GUARD_HIGH / MEMORY_GUARD_MAX or exits 1 before any write.
    # Precedence per value (#2633): CCC_BRIDGE_MEMORY_HIGH/MAX, then the value
    # in an existing ccc-generated drop-in, then the 50%/75% default. Per-node
    # limits live only in that drop-in, so a plain `install` or `memory-guard`
    # rerun without the env must not silently reset them to the default.
    local kept="" prev
    MEMORY_GUARD_HIGH="${CCC_BRIDGE_MEMORY_HIGH:-}"
    if [ -z "$MEMORY_GUARD_HIGH" ]; then
        if prev="$(memory_guard_existing_value MemoryHigh)"; then
            MEMORY_GUARD_HIGH="$prev"; kept="$kept MemoryHigh=$prev"
        else
            MEMORY_GUARD_HIGH="$MEMORY_GUARD_DEFAULT_HIGH"
        fi
    fi
    MEMORY_GUARD_MAX="${CCC_BRIDGE_MEMORY_MAX:-}"
    if [ -z "$MEMORY_GUARD_MAX" ]; then
        if prev="$(memory_guard_existing_value MemoryMax)"; then
            MEMORY_GUARD_MAX="$prev"; kept="$kept MemoryMax=$prev"
        else
            MEMORY_GUARD_MAX="$MEMORY_GUARD_DEFAULT_MAX"
        fi
    fi
    if [ -n "$kept" ]; then
        echo "↩️  keeping the existing memory guard value(s):$kept (set CCC_BRIDGE_MEMORY_HIGH/MAX to change)"
    fi
    local high_kind max_kind
    high_kind="$(memory_limit_kind "$MEMORY_GUARD_HIGH")" || {
        echo "❌ Error: CCC_BRIDGE_MEMORY_HIGH must be 1%..100%, bytes[K|M|G|T], or infinity" >&2
        exit 1
    }
    max_kind="$(memory_limit_kind "$MEMORY_GUARD_MAX")" || {
        echo "❌ Error: CCC_BRIDGE_MEMORY_MAX must be 1%..100%, bytes[K|M|G|T], or infinity" >&2
        exit 1
    }
    local ordered=1
    if [ "$max_kind" = "infinity" ]; then
        ordered=1
    elif [ "$high_kind" = "infinity" ]; then
        ordered=0
    elif [ "$high_kind" = "percent" ] && [ "$max_kind" = "percent" ]; then
        [ "${MEMORY_GUARD_HIGH%\%}" -le "${MEMORY_GUARD_MAX%\%}" ] || ordered=0
    elif [ "$high_kind" = "bytes" ] && [ "$max_kind" = "bytes" ]; then
        memory_limit_not_above "$MEMORY_GUARD_HIGH" "$MEMORY_GUARD_MAX" || ordered=0
    fi
    if [ "$ordered" != "1" ]; then
        echo "❌ Error: CCC_BRIDGE_MEMORY_HIGH ($MEMORY_GUARD_HIGH) must not exceed CCC_BRIDGE_MEMORY_MAX ($MEMORY_GUARD_MAX)" >&2
        exit 1
    fi
}

render_memory_guard_dropin() {
    cat <<DROPIN
$MEMORY_GUARD_MARKER
[Service]
MemoryHigh=${MEMORY_GUARD_HIGH}
MemoryMax=${MEMORY_GUARD_MAX}
OOMPolicy=continue
DROPIN
}

write_memory_guard_dropin() {
    # Returns 0 written, 1 error, 2 already identical, 3 foreign file left alone.
    local dropin_dir="$SYSTEMD_UNIT_FILE.d"
    local dropin="$dropin_dir/$MEMORY_GUARD_DROPIN_NAME"
    local staged
    if [ -e "$dropin" ] || [ -L "$dropin" ]; then
        if [ -L "$dropin" ] || [ ! -f "$dropin" ] \
           || [ "$(head -n 1 "$dropin" 2>/dev/null)" != "$MEMORY_GUARD_MARKER" ]; then
            echo "⚠️  $dropin exists and was not generated by ccc-node; left untouched" >&2
            return 3
        fi
        if cmp -s "$dropin" <(render_memory_guard_dropin); then
            return 2
        fi
    fi
    mkdir -p "$dropin_dir" || return 1
    staged="$(mktemp "$dropin_dir/.${MEMORY_GUARD_DROPIN_NAME}.new.XXXXXX")" || return 1
    if ! render_memory_guard_dropin > "$staged" \
       || ! chmod 0644 "$staged" \
       || ! mv -f -- "$staged" "$dropin"; then
        rm -f -- "$staged"
        return 1
    fi
    return 0
}

remove_memory_guard_dropin() {
    local dropin_dir="$SYSTEMD_UNIT_FILE.d"
    local dropin="$dropin_dir/$MEMORY_GUARD_DROPIN_NAME"
    [ -f "$dropin" ] && [ ! -L "$dropin" ] || return 0
    [ "$(head -n 1 "$dropin" 2>/dev/null)" = "$MEMORY_GUARD_MARKER" ] || return 0
    rm -f -- "$dropin"
    rmdir -- "$dropin_dir" 2>/dev/null || true
}

memory_guard_unit_active() {
    "${SYSTEMCTL[@]}" is-active --quiet "$SYSTEMD_SERVICE" 2>/dev/null
}

memory_guard_set_runtime() {
    # set-property --runtime on the RUNNING unit: no restart, MainPID kept. The
    # values land in /run/systemd/system.control (gone at reboot, when the
    # drop-in alone carries them). Returns 1 on failure.
    local high="$1" max="$2"
    if ! "${SYSTEMCTL[@]}" set-property --runtime "$SYSTEMD_SERVICE" \
            "MemoryHigh=$high" "MemoryMax=$max"; then
        echo "❌ ${SYSTEMCTL[*]} set-property --runtime $SYSTEMD_SERVICE MemoryHigh=$high MemoryMax=$max failed" >&2
        return 1
    fi
    echo "🛡️  runtime limits set without restart: ${SYSTEMCTL[*]} set-property --runtime $SYSTEMD_SERVICE MemoryHigh=$high MemoryMax=$max"
}

memory_guard_verify_cgroup() {
    # Print the effective cgroup memory.high/max of the running unit and
    # compare with what was requested ($1/$2: bytes or infinity). A cgroup still
    # at `max` while a limit was requested is the #2545 half-applied state.
    # Returns 1 only for that state; unverifiable hosts are reported, not failed.
    local want_high="$1" want_max="$2" cg dir high max rc=0
    cg="$("${SYSTEMCTL[@]}" show "$SYSTEMD_SERVICE" -p ControlGroup --value 2>/dev/null)"
    if [ -z "$cg" ]; then
        echo "⚪ cgroup verification skipped: $SYSTEMD_SERVICE reports no ControlGroup"
        return 0
    fi
    dir="$CGROUP_ROOT$cg"
    if [ ! -r "$dir/memory.max" ] || [ ! -r "$dir/memory.high" ]; then
        echo "⚠️  cgroup verification unavailable: $dir/memory.{high,max} not readable (cgroup v1 or no memory controller)"
        return 0
    fi
    high="$(head -n 1 "$dir/memory.high" 2>/dev/null)"
    max="$(head -n 1 "$dir/memory.max" 2>/dev/null)"
    echo "🔎 effective cgroup $dir: memory.high=$high memory.max=$max"
    if [ "$want_high" != "infinity" ] && [ "$high" = "max" ]; then
        echo "❌ memory.high is still 'max' — MemoryHigh NOT applied to the running cgroup" >&2
        rc=1
    fi
    if [ "$want_max" != "infinity" ] && [ "$max" = "max" ]; then
        echo "❌ memory.max is still 'max' — MemoryMax NOT applied to the running cgroup" >&2
        rc=1
    fi
    if [ "$want_high" = "infinity" ] && [ "$high" != "max" ]; then
        echo "⚠️  memory.high=$high but MemoryHigh=infinity was requested (another drop-in or runtime override?)"
    fi
    if [ "$want_max" = "infinity" ] && [ "$max" != "max" ]; then
        echo "⚠️  memory.max=$max but MemoryMax=infinity was requested (another drop-in or runtime override?)"
    fi
    return "$rc"
}

do_memory_guard_remove_systemd() {
    # Rollback mirror of memory-guard: drop the generated file, daemon-reload,
    # and clear the runtime limits on an active unit. OOMPolicy returns to the
    # unit default (stop) with the reload. A foreign file is never touched.
    local dropin="$SYSTEMD_UNIT_FILE.d/$MEMORY_GUARD_DROPIN_NAME"
    if [ "$DRY_RUN" = "1" ]; then
        echo "[dry-run] would remove $dropin (only if generated by ccc-node)"
        echo "[dry-run] then run: ${SYSTEMCTL[*]} daemon-reload (no stop, start, or restart)"
        echo "[dry-run] and, if $SYSTEMD_SERVICE is active: ${SYSTEMCTL[*]} set-property --runtime $SYSTEMD_SERVICE MemoryHigh=infinity MemoryMax=infinity"
        exit 0
    fi
    if [ -e "$dropin" ] || [ -L "$dropin" ]; then
        if [ -L "$dropin" ] || [ ! -f "$dropin" ] \
           || [ "$(head -n 1 "$dropin" 2>/dev/null)" != "$MEMORY_GUARD_MARKER" ]; then
            echo "⚠️  $dropin exists and was not generated by ccc-node; left untouched" >&2
            exit 1
        fi
        remove_memory_guard_dropin
        echo "🗑️  removed memory guard drop-in: $dropin"
    else
        echo "⚪ no memory guard drop-in to remove: $dropin"
    fi
    if ! "${SYSTEMCTL[@]}" daemon-reload; then
        echo "❌ daemon-reload failed after removing the drop-in" >&2
        exit 1
    fi
    if ! memory_guard_unit_active; then
        echo "⚪ $SYSTEMD_SERVICE is not active; no runtime limits to clear"
        exit 0
    fi
    memory_guard_set_runtime infinity infinity || exit 1
    memory_guard_verify_cgroup infinity infinity || true
    echo "✅ memory guard rolled back without restart (runtime infinity stays in system.control until reboot)"
    exit 0
}

do_memory_guard_systemd() {
    systemd_paths
    [ "$REMOVE" = "1" ] || resolve_memory_guard
    if [ -z "${CCC_SYSTEMD_DIR:-}" ] && ! command -v "$SYSTEMCTL_BIN" >/dev/null 2>&1; then
        echo "⚪ systemctl not found — no systemd on this host; memory guard not applicable"
        exit 0
    fi
    if [ ! -f "$SYSTEMD_UNIT_FILE" ]; then
        echo "⚪ systemd unit not installed; memory guard skipped: $SYSTEMD_UNIT_FILE"
        exit 0
    fi
    [ "$REMOVE" = "1" ] && do_memory_guard_remove_systemd
    local dropin="$SYSTEMD_UNIT_FILE.d/$MEMORY_GUARD_DROPIN_NAME"
    local runtime_high runtime_max
    if ! runtime_high="$(memory_limit_runtime_value "$MEMORY_GUARD_HIGH")" \
       || ! runtime_max="$(memory_limit_runtime_value "$MEMORY_GUARD_MAX")"; then
        echo "❌ Error: cannot resolve MemoryHigh=$MEMORY_GUARD_HIGH / MemoryMax=$MEMORY_GUARD_MAX to bytes (MemTotal unreadable in $MEMINFO_FILE); nothing written" >&2
        exit 1
    fi
    if [ "$DRY_RUN" = "1" ]; then
        echo "[dry-run] would write $dropin:"
        render_memory_guard_dropin | sed 's/^/[dry-run]   /'
        echo "[dry-run] then run: ${SYSTEMCTL[*]} daemon-reload (no stop, start, or restart)"
        echo "[dry-run] and, if $SYSTEMD_SERVICE is active: ${SYSTEMCTL[*]} set-property --runtime $SYSTEMD_SERVICE MemoryHigh=$runtime_high MemoryMax=$runtime_max"
        exit 0
    fi
    local status=0
    write_memory_guard_dropin || status=$?
    case "$status" in
        2)
            # The file can be current while the running cgroup is not (a
            # drop-in written by an older memory-guard that only reloaded), so
            # fall through to the runtime step instead of exiting here.
            echo "✅ memory guard drop-in already current; left untouched: $dropin"
            ;;
        3) exit 1 ;;
        0)
            if ! command -v "$SYSTEMCTL_BIN" >/dev/null 2>&1 || ! "${SYSTEMCTL[@]}" daemon-reload; then
                echo "❌ daemon-reload failed; drop-in written but not yet loaded: $dropin" >&2
                exit 1
            fi
            echo "✅ memory guard installed (MemoryHigh=$MEMORY_GUARD_HIGH MemoryMax=$MEMORY_GUARD_MAX OOMPolicy=continue); service not restarted: $dropin"
            ;;
        *)
            echo "❌ Failed to write memory guard drop-in: $dropin" >&2
            exit 1
            ;;
    esac
    # daemon-reload does not push MemoryHigh/MemoryMax into a running cgroup
    # (#2545: a live bridge stayed at memory.high/max=max after the reload).
    if ! memory_guard_unit_active; then
        echo "⚪ $SYSTEMD_SERVICE is not active; the limits apply on its next start (no runtime change)"
        # A runtime value from an earlier set-property (e.g. memory-guard
        # --remove) sorts after the drop-in and would win at that start.
        local loaded_max
        loaded_max="$("${SYSTEMCTL[@]}" show "$SYSTEMD_SERVICE" -p MemoryMax --value 2>/dev/null)"
        if [ "$runtime_max" != "infinity" ] && [ "$loaded_max" = "infinity" ]; then
            echo "⚠️  loaded MemoryMax=infinity: a runtime override in system.control shadows the drop-in until reboot; start the unit and rerun memory-guard" >&2
        fi
        exit 0
    fi
    memory_guard_set_runtime "$runtime_high" "$runtime_max" || exit 1
    memory_guard_verify_cgroup "$runtime_high" "$runtime_max" || exit 1
    exit 0
}

do_install_systemd() {
    if ! command -v "$SYSTEMCTL_BIN" >/dev/null 2>&1; then
        echo "❌ systemctl not found — this host does not use systemd. On macOS use --install instead."
        exit 1
    fi
    if [ -z "$PROJECT_ROOT_ARG" ]; then
        echo "❌ Error: --project-root is required"
        exit 1
    fi
    local PROJECT_ROOT
    PROJECT_ROOT="$(cd "$PROJECT_ROOT_ARG" 2>/dev/null && pwd)" || {
        echo "❌ Error: Project path does not exist: $PROJECT_ROOT_ARG"
        exit 1
    }
    validate_render_home
    local memory_guard=1
    [ "${CCC_BRIDGE_MEMORY_GUARD:-1}" = "0" ] && memory_guard=0
    systemd_paths
    # Validate the guard values before writing anything. After systemd_paths
    # so an existing drop-in's per-node values are kept (#2633).
    [ "$memory_guard" = "1" ] && resolve_memory_guard

    echo "📝 Generating systemd unit: $SYSTEMD_UNIT_FILE"
    mkdir -p "$SYSTEMD_UNIT_DIR"
    render_systemd_unit "$PROJECT_ROOT" "$PROXY_URL_ARG" > "$SYSTEMD_UNIT_FILE"
    if [ "$memory_guard" = "1" ]; then
        local guard_status=0
        write_memory_guard_dropin || guard_status=$?
        case "$guard_status" in
            0|2) echo "🛡️  Memory guard: MemoryHigh=$MEMORY_GUARD_HIGH MemoryMax=$MEMORY_GUARD_MAX OOMPolicy=continue ($SYSTEMD_UNIT_FILE.d/$MEMORY_GUARD_DROPIN_NAME)" ;;
            3) ;;
            *) echo "⚠️  Memory guard drop-in could not be written; unit installed without it" >&2 ;;
        esac
    fi

    "${SYSTEMCTL[@]}" daemon-reload
    if "${SYSTEMCTL[@]}" enable --now "$SYSTEMD_SERVICE"; then
        echo "✅ Installed and started as $SYSTEMD_SCOPE service: $SYSTEMD_SERVICE"
    else
        echo "⚠️  enable --now failed; unit written to $SYSTEMD_UNIT_FILE — inspect with: ${SYSTEMCTL[*]} status $SYSTEMD_SERVICE"
        exit 1
    fi
    local journal_scope=""
    [ "$SYSTEMD_SCOPE" = "user" ] && journal_scope="--user "
    echo "💡 Status: ${SYSTEMCTL[*]} status $SYSTEMD_SERVICE"
    echo "💡 Logs:   journalctl ${journal_scope}-u $SYSTEMD_SERVICE -f"
    echo "💡 Remove: $CALLER --path \"$PROJECT_ROOT\" --uninstall-systemd"
    exit 0
}

is_supported_generated_unit() {
    # Reconciliation is intentionally bounded to the schema historically
    # emitted by this script. Unknown directives and node-local Environment=
    # entries are not copied into the canonical main unit: operators should
    # keep those in <unit>.d/*.conf drop-ins. This leaves bespoke units such as
    # #831 untouched pending an explicit normalization decision.
    local line
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            ""|"#"*|"[Unit]"|"[Service]"|"[Install]") ;;
            "Description=ccc-node Telegram bridge ("*")") ;;
            "After=network-online.target"|"Wants=network-online.target") ;;
            "Type=simple"|"WorkingDirectory="*) ;;
            "Environment=HOME="*|"Environment=PATH="*) ;;
            "EnvironmentFile=-/"*"/.config/ccc-node/bridge.env") ;;
            "Environment=http_proxy="*|"Environment=https_proxy="*) ;;
            "Environment=all_proxy="*|"Environment=no_proxy="*) ;;
            "ExecStart=/bin/bash "*"start.sh --path "*) ;;
            "Restart="*|"RestartSec="*|"KillMode="*|"SendSIGKILL="*|"TimeoutStopSec="*) ;;
            "WantedBy=multi-user.target"|"WantedBy=default.target") ;;
            *) return 1 ;;
        esac
    done < "$SYSTEMD_UNIT_FILE"

    # Require the complete generated-unit skeleton exactly once. The directive
    # allowlist above prevents unknown policy from being copied; these counts
    # prevent a coincidentally similar hand-written fragment from being
    # mistaken for an old generated unit.
    [ "$(grep -Fxc '[Unit]' "$SYSTEMD_UNIT_FILE")" = "1" ] \
        && [ "$(grep -Fxc '[Service]' "$SYSTEMD_UNIT_FILE")" = "1" ] \
        && [ "$(grep -Fxc '[Install]' "$SYSTEMD_UNIT_FILE")" = "1" ] \
        && [ "$(grep -c '^Description=ccc-node Telegram bridge (' "$SYSTEMD_UNIT_FILE")" = "1" ] \
        && [ "$(grep -Fxc 'After=network-online.target' "$SYSTEMD_UNIT_FILE")" = "1" ] \
        && [ "$(grep -Fxc 'Wants=network-online.target' "$SYSTEMD_UNIT_FILE")" = "1" ] \
        && [ "$(grep -Fxc 'Type=simple' "$SYSTEMD_UNIT_FILE")" = "1" ] \
        && [ "$(grep -c '^WorkingDirectory=' "$SYSTEMD_UNIT_FILE")" = "1" ] \
        && [ "$(grep -c '^Environment=HOME=' "$SYSTEMD_UNIT_FILE")" = "1" ] \
        && [ "$(grep -c '^Environment=PATH=' "$SYSTEMD_UNIT_FILE")" = "1" ] \
        && [ "$(grep -c '^EnvironmentFile=' "$SYSTEMD_UNIT_FILE")" -le "1" ] \
        && [ "$(grep -c '^ExecStart=' "$SYSTEMD_UNIT_FILE")" = "1" ] \
        && [ "$(grep -c '^Restart=' "$SYSTEMD_UNIT_FILE")" = "1" ] \
        && [ "$(grep -c '^RestartSec=' "$SYSTEMD_UNIT_FILE")" = "1" ] \
        && [ "$(grep -c '^KillMode=' "$SYSTEMD_UNIT_FILE")" -le "1" ] \
        && [ "$(grep -c '^SendSIGKILL=' "$SYSTEMD_UNIT_FILE")" -le "1" ] \
        && [ "$(grep -c '^TimeoutStopSec=' "$SYSTEMD_UNIT_FILE")" = "1" ] \
        && [ "$(grep -Ec '^WantedBy=(multi-user|default)\.target$' "$SYSTEMD_UNIT_FILE")" = "1" ]
}

installed_render_inputs() {
    local exec_line project_root installed_repo
    local http_proxy="" https_proxy="" all_proxy="" no_proxy=""
    local exec_count proxy_count

    exec_count="$(grep -c '^ExecStart=' "$SYSTEMD_UNIT_FILE" 2>/dev/null || true)"
    [ "$exec_count" = "1" ] || return 1
    exec_line="$(grep '^ExecStart=' "$SYSTEMD_UNIT_FILE")"
    project_root="$(printf '%s\n' "$exec_line" \
        | sed -n 's|^ExecStart=/bin/bash /.*/start\.sh --path \(/.*\)$|\1|p')"
    [ -n "$project_root" ] || return 1
    project_root="$(cd "$project_root" 2>/dev/null && pwd)" || return 1

    # The checkout the installed unit boots from, kept as the literal recorded in
    # the unit. It is deliberately NOT resolved through `cd`: a unit pointing at
    # a deleted work tree must still compare unequal to this checkout so the
    # relocation guard fires, rather than failing the whole recognition step and
    # reporting the unit as unrecognized.
    installed_repo="$(printf '%s\n' "$exec_line" \
        | sed -n 's|^ExecStart=/bin/bash \(/.*\)/bridge/start\.sh --path /.*$|\1|p')"
    [ -n "$installed_repo" ] || return 1

    proxy_count="$(grep -Ec '^Environment=(http_proxy|https_proxy|all_proxy|no_proxy)=' \
        "$SYSTEMD_UNIT_FILE" 2>/dev/null || true)"
    if [ "$proxy_count" != "0" ]; then
        [ "$proxy_count" = "4" ] || return 1
        http_proxy="$(sed -n 's/^Environment=http_proxy=//p' "$SYSTEMD_UNIT_FILE")"
        https_proxy="$(sed -n 's/^Environment=https_proxy=//p' "$SYSTEMD_UNIT_FILE")"
        all_proxy="$(sed -n 's/^Environment=all_proxy=//p' "$SYSTEMD_UNIT_FILE")"
        no_proxy="$(sed -n 's/^Environment=no_proxy=//p' "$SYSTEMD_UNIT_FILE")"
        [ -n "$http_proxy" ] || return 1
        [ "$http_proxy" = "$https_proxy" ] && [ "$http_proxy" = "$all_proxy" ] || return 1
        [ "$no_proxy" = "localhost,127.0.0.1,192.168.0.0/16,10.0.0.0/8,172.16.0.0/12" ] || return 1
    fi

    RECONCILE_PROJECT_ROOT="$project_root"
    RECONCILE_PROXY_URL="$http_proxy"
    RECONCILE_INSTALLED_REPO="$installed_repo"
}

is_canonical_unit_topology() {
    if [ -L "$SYSTEMD_UNIT_FILE" ]; then
        echo "⚠️  Existing unit path is a symlink and is treated as noncanonical/bespoke; left untouched: $SYSTEMD_UNIT_FILE" >&2
        echo "    Normalize the main-unit path explicitly and keep node-local overrides in $SYSTEMD_UNIT_FILE.d/*.conf drop-ins." >&2
        return 1
    fi

    local link_count
    if ! link_count="$(stat -c '%h' -- "$SYSTEMD_UNIT_FILE" 2>/dev/null)" \
       || ! [[ "$link_count" =~ ^[1-9][0-9]*$ ]]; then
        echo "❌ Cannot trust link-count metadata for the existing unit; left untouched: $SYSTEMD_UNIT_FILE" >&2
        return 2
    fi
    if [ "$link_count" != "1" ]; then
        echo "⚠️  Existing unit has $link_count hard links and is treated as noncanonical/bespoke; left untouched: $SYSTEMD_UNIT_FILE" >&2
        echo "    Normalize the main-unit path explicitly and keep node-local overrides in $SYSTEMD_UNIT_FILE.d/*.conf drop-ins." >&2
        return 1
    fi
}

do_reconcile_systemd() {
    validate_render_home
    systemd_paths
    if [ ! -f "$SYSTEMD_UNIT_FILE" ]; then
        echo "⚪ systemd unit not installed; reconciliation skipped: $SYSTEMD_UNIT_FILE"
        exit 0
    fi
    is_canonical_unit_topology
    local topology_status=$?
    if [ "$topology_status" = "1" ]; then
        exit 0
    elif [ "$topology_status" != "0" ]; then
        exit 1
    fi
    if ! is_supported_generated_unit || ! installed_render_inputs; then
        echo "⚠️  Existing unit is not a recognized ccc-generated main unit; left untouched: $SYSTEMD_UNIT_FILE" >&2
        echo "    Normalize it explicitly and keep node-local overrides in $SYSTEMD_UNIT_FILE.d/*.conf drop-ins." >&2
        exit 0
    fi

    # The renderer builds ExecStart/WorkingDirectory from THIS checkout's own
    # location, so reconciling from a work tree does not repair the unit — it
    # moves the service onto whatever branch that tree happens to hold. setup.sh
    # calls reconcile unconditionally, which makes every `./setup.sh` run inside
    # a scratch checkout a silent takeover of the node's live bridge.
    #
    # Both halves of that were observed on 2026-08-01: seoseo had been serving
    # from /work/agent-codebench/ccc-node-pr833 (a PR head that never reached
    # main, five commits behind it) since a 06:50 restart, and bangtong's unit
    # had been repointed at /root/ccc-node-840-terminal-stall while its bridge
    # still ran from /opt — one restart away from the same fate. See issue #842.
    #
    # Reconciling drift in place is in scope. Relocating to a different checkout
    # is a deliberate act and has to say so.
    if [ "$RECONCILE_INSTALLED_REPO" != "$REPO_ROOT" ] && [ "$ALLOW_RELOCATE" != "1" ]; then
        echo "⚠️  Installed unit boots from a different checkout; left untouched: $SYSTEMD_UNIT_FILE" >&2
        echo "    installed unit: $RECONCILE_INSTALLED_REPO" >&2
        echo "    this checkout:  $REPO_ROOT" >&2
        echo "    Reconcile repairs drift; it does not relocate the service. If moving the" >&2
        echo "    bridge onto this checkout is intended, say so explicitly:" >&2
        echo "      $0 reconcile --allow-relocate" >&2
        exit 0
    fi

    local candidate
    candidate="$(rendered_unit_value "$RECONCILE_PROJECT_ROOT" "$RECONCILE_PROXY_URL")"
    if cmp -s "$SYSTEMD_UNIT_FILE" <(printf '%s\n' "$candidate"); then
        echo "✅ systemd unit already canonical; left untouched: $SYSTEMD_UNIT_FILE"
        exit 0
    fi
    if [ "$DRY_RUN" = "1" ]; then
        echo "[dry-run] systemd unit drift detected: $SYSTEMD_UNIT_FILE"
        echo "[dry-run] would atomically replace the canonical main unit and run: ${SYSTEMCTL[*]} daemon-reload"
        echo "[dry-run] service enabled/active state would be preserved; no stop, start, enable, or restart"
        exit 0
    fi
    if ! command -v "$SYSTEMCTL_BIN" >/dev/null 2>&1; then
        echo "❌ systemctl not found; existing unit left untouched: $SYSTEMD_UNIT_FILE" >&2
        exit 1
    fi

    local staged backup
    staged="$(mktemp "$SYSTEMD_UNIT_DIR/.${SYSTEMD_SERVICE}.new.XXXXXX")" || {
        echo "❌ Failed to stage canonical systemd unit; existing unit left untouched" >&2
        exit 1
    }
    backup="$(mktemp "$SYSTEMD_UNIT_DIR/.${SYSTEMD_SERVICE}.rollback.XXXXXX")" || {
        rm -f -- "$staged"
        echo "❌ Failed to stage systemd rollback copy; existing unit left untouched" >&2
        exit 1
    }
    if ! printf '%s\n' "$candidate" > "$staged" \
       || ! chmod 0644 "$staged" \
       || ! cp -p -- "$SYSTEMD_UNIT_FILE" "$backup" \
       || ! cmp -s "$SYSTEMD_UNIT_FILE" "$backup"; then
        rm -f -- "$staged" "$backup"
        echo "❌ Failed to stage systemd reconciliation; existing unit left untouched" >&2
        exit 1
    fi
    if ! mv -f -- "$staged" "$SYSTEMD_UNIT_FILE"; then
        rm -f -- "$staged" "$backup"
        echo "❌ Atomic systemd unit replacement failed; existing unit left untouched" >&2
        exit 1
    fi
    if ! "${SYSTEMCTL[@]}" daemon-reload; then
        echo "❌ systemctl daemon-reload failed; restoring the previous unit" >&2
        if ! mv -f -- "$backup" "$SYSTEMD_UNIT_FILE"; then
            echo "❌ Fail-closed rollback degraded; recovery copy retained at: $backup" >&2
            exit 1
        fi
        if ! "${SYSTEMCTL[@]}" daemon-reload; then
            echo "❌ Previous unit restored, but rollback daemon-reload also failed; service was not restarted" >&2
            exit 1
        fi
        echo "❌ Previous unit restored and reloaded; service was not restarted" >&2
        exit 1
    fi
    rm -f -- "$backup"
    echo "✅ Reconciled canonical $SYSTEMD_SCOPE unit without restarting or changing service state: $SYSTEMD_UNIT_FILE"
    exit 0
}

do_uninstall_systemd() {
    if ! command -v "$SYSTEMCTL_BIN" >/dev/null 2>&1; then
        echo "❌ systemctl not found — nothing to uninstall."
        exit 1
    fi
    systemd_paths
    if [ -f "$SYSTEMD_UNIT_FILE" ]; then
        echo "🗑️  Removing systemd unit: $SYSTEMD_UNIT_FILE"
        "${SYSTEMCTL[@]}" disable --now "$SYSTEMD_SERVICE" 2>/dev/null || true
        rm -f "$SYSTEMD_UNIT_FILE"
        remove_memory_guard_dropin
        "${SYSTEMCTL[@]}" daemon-reload
        echo "✅ systemd service uninstalled"
    else
        echo "⚪ systemd service not installed ($SYSTEMD_UNIT_FILE not found)"
    fi
    exit 0
}

do_is_managed() {
    # Conservative ownership probe for start.sh --restart: report "managed"
    # only when the unit file for this scope exists AND systemctl says the
    # service is active. Anything less confident falls through to a normal
    # process-level restart. Honors the CCC_SYSTEMD_DIR / CCC_SYSTEMCTL test
    # seams via systemd_paths / SYSTEMCTL_BIN like install/uninstall.
    command -v "$SYSTEMCTL_BIN" >/dev/null 2>&1 || exit 1
    systemd_paths
    [ -f "$SYSTEMD_UNIT_FILE" ] || exit 1
    if "${SYSTEMCTL[@]}" is-active --quiet "$SYSTEMD_SERVICE" 2>/dev/null; then
        echo "managed: $SYSTEMD_UNIT_FILE (active)"
        exit 0
    fi
    exit 1
}

do_main_pid() {
    # Print the MainPID of the active managed bridge unit for this scope, or
    # exit 1 with no output. Same conservative ownership rule as is-managed:
    # systemctl available, unit file present for this scope, service active.
    # start.sh --status uses this to recognize a service-managed bot whose pid
    # file was lost to the concurrent-instance race as "available" rather than
    # "degraded". Honors the CCC_SYSTEMD_DIR / CCC_SYSTEMCTL test seams.
    command -v "$SYSTEMCTL_BIN" >/dev/null 2>&1 || exit 1
    systemd_paths
    [ -f "$SYSTEMD_UNIT_FILE" ] || exit 1
    "${SYSTEMCTL[@]}" is-active --quiet "$SYSTEMD_SERVICE" 2>/dev/null || exit 1
    local main_pid
    main_pid="$("${SYSTEMCTL[@]}" show "$SYSTEMD_SERVICE" -p MainPID --value 2>/dev/null)"
    [ -n "$main_pid" ] && [ "$main_pid" != "0" ] || exit 1
    printf '%s\n' "$main_pid"
    exit 0
}

case "$SUBCOMMAND" in
    install)    do_install_systemd ;;
    reconcile)  do_reconcile_systemd ;;
    memory-guard) do_memory_guard_systemd ;;
    uninstall)  do_uninstall_systemd ;;
    is-managed) do_is_managed ;;
    main-pid)   do_main_pid ;;
esac
