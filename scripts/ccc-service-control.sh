#!/usr/bin/env bash
# Root-installed, exact-allowlist service restart wrapper.
# See docs/service-control.md.  Do not grant sudo to a mutable checkout copy.
set -uo pipefail

die() { printf 'ccc-service-control: %s\n' "$*" >&2; exit 2; }

[ "$#" -eq 2 ] || die 'usage: ccc-service-control restart <unit.service>'
action="$1"
unit="$2"
[ "$action" = restart ] || die 'only restart is supported'
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

allowed=0
while IFS= read -r line || [ -n "$line" ]; do
  line="${line%%#*}"
  line="${line#"${line%%[![:space:]]*}"}"
  line="${line%"${line##*[![:space:]]}"}"
  [ -n "$line" ] || continue
  printf '%s' "$line" | grep -Eq '^[A-Za-z0-9_.@:-]+\.service$' \
    || die 'allowlist contains an invalid unit entry'
  [ "$line" = "$unit" ] && allowed=1
done < "$allowlist"
[ "$allowed" -eq 1 ] || die "unit is not allowlisted: $unit"

if [ "$dry_run" = 1 ]; then
  printf 'DRY-RUN: /usr/bin/systemctl restart -- %s\n' "$unit"
  exit 0
fi

[ "$(id -u)" -eq 0 ] || die 'production restart must run as root via the installed wrapper'
[ "$script_uid" -eq 0 ] || die 'production wrapper must be owned by root'
exec /usr/bin/systemctl restart -- "$unit"
