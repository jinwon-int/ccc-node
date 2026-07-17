#!/usr/bin/env bash
# Root-installed broker Compose reconciliation wrapper.
#
# Encapsulates the fixed broker runbook (cd project dir, capture the git
# revision for image labelling, `docker compose up -d <allowlisted services>`)
# behind a single operator-owned entrypoint, so the runbook need NOT be
# expressed as fragile ALLOW-grammar inside the PreToolUse guard. The guard
# still denies raw `docker compose up`; it allows this wrapper by name only
# because the wrapper is root-owned and the agent cannot alter what it does.
# New runbook needs are added HERE (reviewed), not as new guard grammar.
#
# See docs/service-control.md. Do not grant to a mutable checkout copy.
set -uo pipefail

die() { printf 'ccc-broker-reconcile: %s\n' "$*" >&2; exit 2; }

[ "$#" -ge 1 ] || die 'usage: ccc-broker-reconcile <service> [<service>...]'

# --- integrity: the wrapper must be a root-owned, non-writable, non-symlink file
script_path="$(readlink -f -- "$0" 2>/dev/null)" || die 'cannot resolve wrapper path'
{ [ -f "$script_path" ] && [ ! -L "$0" ]; } || die 'wrapper must be a regular non-symlink file'
script_uid="$(stat -c '%u' -- "$script_path" 2>/dev/null)" || die 'cannot stat wrapper'
script_mode="$(stat -c '%a' -- "$script_path" 2>/dev/null)" || die 'cannot stat wrapper mode'
script_group_digit=$(( (10#$script_mode / 10) % 10 ))
script_other_digit=$(( 10#$script_mode % 10 ))
(( (script_group_digit & 2) == 0 && (script_other_digit & 2) == 0 )) \
  || die 'wrapper is group/world writable'

# Production always uses the fixed root-owned config paths. The overrides are
# accepted only in dry-run mode so tests can validate policy without privilege
# or touching a live broker; dry-run can only reduce capability.
dry_run="${CCC_BROKER_RECONCILE_DRY_RUN:-0}"
if [ "$dry_run" = 1 ]; then
  dir_file="${CCC_BROKER_RECONCILE_DIR_FILE:-/etc/ccc-node/broker-reconcile.dir}"
  allowlist="${CCC_BROKER_RECONCILE_ALLOWLIST:-/etc/ccc-node/broker-reconcile.allow}"
else
  dir_file='/etc/ccc-node/broker-reconcile.dir'
  allowlist='/etc/ccc-node/broker-reconcile.allow'
fi

# --- validate an operator-owned config file: regular, non-symlink, owned by the
#     wrapper owner, not group/world writable (identical trust to the wrapper).
validate_conf() { # <path>
  local p="$1" uid mode g o
  [ -e "$p" ] || die "config missing: $p"
  [ ! -L "$p" ] || die "config must not be a symlink: $p"
  [ -f "$p" ] || die "config must be a regular file: $p"
  uid="$(stat -c '%u' -- "$p" 2>/dev/null)" || die "cannot stat config: $p"
  [ "$uid" = "$script_uid" ] || die "config owner must match wrapper owner: $p"
  mode="$(stat -c '%a' -- "$p" 2>/dev/null)" || die "cannot stat config mode: $p"
  g=$(( (10#$mode / 10) % 10 ))
  o=$(( 10#$mode % 10 ))
  (( (g & 2) == 0 && (o & 2) == 0 )) || die "config is group/world writable: $p"
}
validate_conf "$dir_file"
validate_conf "$allowlist"

# --- resolve the operator-fixed broker project dir (absolute, safe chars) ---
broker_dir="$(sed -e 's/#.*//' "$dir_file" | grep -m1 -E '^[[:space:]]*/' || true)"
broker_dir="${broker_dir#"${broker_dir%%[![:space:]]*}"}"
broker_dir="${broker_dir%"${broker_dir##*[![:space:]]}"}"
[ -n "$broker_dir" ] || die "no absolute broker dir configured in $dir_file"
case "$broker_dir" in
  /*) : ;;
  *) die 'broker dir must be absolute' ;;
esac
case "$broker_dir" in
  *[!A-Za-z0-9_/.@:-]*) die 'broker dir has unsafe characters' ;;
esac

# --- load the operator service allowlist ---
allow=()
while IFS= read -r line || [ -n "$line" ]; do
  line="${line%%#*}"
  line="${line#"${line%%[![:space:]]*}"}"
  line="${line%"${line##*[![:space:]]}"}"
  [ -n "$line" ] || continue
  printf '%s' "$line" | grep -Eq '^[A-Za-z0-9_.@:-]+$' \
    || die "allowlist contains an invalid service entry: $line"
  allow+=("$line")
done < "$allowlist"
[ "${#allow[@]}" -gt 0 ] || die "allowlist has no services: $allowlist"

# --- every requested service must be a valid token AND allowlisted ---
for svc in "$@"; do
  printf '%s' "$svc" | grep -Eq '^[A-Za-z0-9_.@:-]+$' || die "invalid service token: $svc"
  ok=0
  for a in "${allow[@]}"; do [ "$a" = "$svc" ] && ok=1; done
  [ "$ok" -eq 1 ] || die "service is not allowlisted: $svc"
done

if [ "$dry_run" = 1 ]; then
  printf 'DRY-RUN: cd %s && export A2A_BROKER_REVISION=$(git rev-parse HEAD) && docker compose up -d' "$broker_dir"
  printf ' %s' "$@"
  printf '\n'
  exit 0
fi

[ -d "$broker_dir" ] || die "broker dir does not exist: $broker_dir"
cd "$broker_dir" || die "cannot cd to broker dir: $broker_dir"
A2A_BROKER_REVISION="$(git rev-parse HEAD 2>/dev/null)" || die 'git rev-parse HEAD failed (broker dir not a git repo?)'
export A2A_BROKER_REVISION
exec docker compose up -d "$@"
