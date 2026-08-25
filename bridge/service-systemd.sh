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
#   service-systemd.sh uninstall [--caller <name>]
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

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SYSTEMCTL_BIN="${CCC_SYSTEMCTL:-systemctl}"

usage() {
    cat <<EOF
Usage: $0 <install|reconcile|uninstall|is-managed|main-pid> [options]

Subcommands:
  install     Generate the systemd unit and enable --now it
  reconcile   If an existing ccc-generated unit has drifted, atomically replace
              its canonical main unit and daemon-reload without changing its
              enabled/active state or restarting the bridge
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
  --dry-run             Report reconcile actions without writing or systemctl
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

while [ $# -gt 0 ]; do
    case "$1" in
        install|reconcile|uninstall|is-managed|main-pid)
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
if [ "$DRY_RUN" = "1" ] && [ "$SUBCOMMAND" != "reconcile" ]; then
    echo "❌ Error: --dry-run is supported only with reconcile" >&2
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
    systemd_paths

    echo "📝 Generating systemd unit: $SYSTEMD_UNIT_FILE"
    mkdir -p "$SYSTEMD_UNIT_DIR"
    render_systemd_unit "$PROJECT_ROOT" "$PROXY_URL_ARG" > "$SYSTEMD_UNIT_FILE"

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
    uninstall)  do_uninstall_systemd ;;
    is-managed) do_is_managed ;;
    main-pid)   do_main_pid ;;
esac
