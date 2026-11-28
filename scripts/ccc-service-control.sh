#!/usr/bin/env bash
# Root-installed, exact-allowlist service lifecycle wrapper.
# See docs/service-control.md.  Do not grant sudo to a mutable checkout copy.
#
# Actions: restart | start | stop | status (#2892 B1). Each allowlist line is
#   <unit.service>                 -> restart,status (the pre-#2892 meaning)
#   <unit.service> <verb[,verb]>   -> exactly those verbs
# status is read-only and prints a fixed set of `systemctl show` properties.
set -uo pipefail

die() { printf 'ccc-service-control: %s\n' "$*" >&2; exit 2; }

usage='usage: ccc-service-control <restart|start|stop|status> <unit.service>'
[ "$#" -eq 2 ] || die "$usage"
action="$1"
unit="$2"
case "$action" in
  restart|start|stop|status) ;;
  *) die "unsupported action: $action ($usage)" ;;
esac
printf '%s' "$unit" | grep -Eq '^[A-Za-z0-9_.@:-]+\.service$' \
  || die 'unit must be an exact .service name'

script_path="$(readlink -f -- "$0" 2>/dev/null)" || die 'cannot resolve wrapper path'
[ -f "$script_path" ] && [ ! -L "$0" ] || die 'wrapper must be a regular non-symlink file'
script_uid="$(stat -c '%u' -- "$script_path" 2>/dev/null)" || die 'cannot stat wrapper'
script_mode="$(stat -c '%a' -- "$script_path" 2>/dev/null)" || die 'cannot stat wrapper mode'
script_group_digit=$(( (10#$script_mode / 10) % 10 ))
script_other_digit=$(( 10#$script_mode % 10 ))
(( (script_group_digit & 2) == 0 && (script_other_digit & 2) == 0 )) \
  || die 'wrapper is group/world writable'

# Production execution always uses the fixed root-owned path.  The override is
# accepted only in dry-run mode so tests can validate policy without privilege or
# touching a live service; dry-run can only reduce capability.
dry_run="${CCC_SERVICE_CONTROL_DRY_RUN:-0}"
if [ "$dry_run" = 1 ]; then
  allowlist="${CCC_SERVICE_CONTROL_ALLOWLIST:-/etc/ccc-node/service-control.allow}"
else
  allowlist='/etc/ccc-node/service-control.allow'
fi

[ -e "$allowlist" ] || die "allowlist missing: $allowlist"
[ ! -L "$allowlist" ] || die 'allowlist must not be a symlink'
[ -f "$allowlist" ] || die 'allowlist must be a regular file'
allow_uid="$(stat -c '%u' -- "$allowlist" 2>/dev/null)" || die 'cannot stat allowlist'
[ "$allow_uid" = "$script_uid" ] || die 'allowlist owner must match installed wrapper owner'
allow_mode="$(stat -c '%a' -- "$allowlist" 2>/dev/null)" || die 'cannot stat allowlist mode'
allow_group_digit=$(( (10#$allow_mode / 10) % 10 ))
allow_other_digit=$(( 10#$allow_mode % 10 ))
(( (allow_group_digit & 2) == 0 && (allow_other_digit & 2) == 0 )) \
  || die 'allowlist is group/world writable'

listed=0
verbs=''
while IFS= read -r line || [ -n "$line" ]; do
  line="${line%%#*}"
  line="${line#"${line%%[![:space:]]*}"}"
  line="${line%"${line##*[![:space:]]}"}"
  [ -n "$line" ] || continue
  printf '%s' "$line" \
    | grep -Eq '^[A-Za-z0-9_.@:-]+\.service([[:space:]]+(restart|start|stop|status)(,(restart|start|stop|status))*)?$' \
    || die 'allowlist contains an invalid entry'
  entry_unit="${line%%[[:space:]]*}"
  [ "$entry_unit" = "$unit" ] || continue
  [ "$listed" -eq 0 ] || die "allowlist lists a unit twice: $unit"
  listed=1
  if [ "$entry_unit" = "$line" ]; then
    verbs='restart,status'
  else
    verbs="${line#"$entry_unit"}"
    verbs="${verbs#"${verbs%%[![:space:]]*}"}"
  fi
done < "$allowlist"
[ "$listed" -eq 1 ] || die "unit is not allowlisted: $unit"
case ",$verbs," in
  *",$action,"*) ;;
  *) die "action not allowlisted for $unit: $action" ;;
esac

if [ "$action" = status ]; then
  cmd=(/usr/bin/systemctl show --no-pager
       "--property=Id,LoadState,ActiveState,SubState,UnitFileState,MainPID,ActiveEnterTimestamp,NRestarts"
       -- "$unit")
else
  cmd=(/usr/bin/systemctl "$action" -- "$unit")
fi

if [ "$dry_run" = 1 ]; then
  printf 'DRY-RUN: %s\n' "${cmd[*]}"
  exit 0
fi

[ "$(id -u)" -eq 0 ] || die "production $action must run as root via the installed wrapper"
[ "$script_uid" -eq 0 ] || die 'production wrapper must be owned by root'
case "$action" in
  status)
    exec "${cmd[@]}"
    ;;
  restart)
    # ccc-side-effect: service_control.restart
    exec "${cmd[@]}"
    ;;
  start|stop)
    # ccc-side-effect: service_control.lifecycle
    exec "${cmd[@]}"
    ;;
esac
die "unreachable action: $action"
