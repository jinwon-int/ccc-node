#!/usr/bin/env bash
# Honcho peer corpus staleness canary (#1263 follow-up).
#
# Read-only: aggregates document dates only, never selects or prints document
# content. Detects the "frozen member-peer corpus" failure mode seen on
# 2026-08-24 (jingun/daegyo dialectic answers confidently citing 6-week-old
# derived facts because ingestion stopped while the deriver queue stayed
# empty — staleness there is invisible to queue-depth monitoring).
#
# Runs against the central Honcho database through a read-only psql command.
# The SQL text is piped on stdin (never argv) so quoting survives every
# SSH/shell layer verbatim; override CCC_HONCHO_STALENESS_PSQL_CMD for tests
# and non-standard nodes.
#
# Exit codes: 0 = all fresh/exempt, 1 = stale peer(s) found, 2 = query failed.
#
# Node activation is operator-placed (self-update/live-backups-rotate precedent:
# the line is hand-installed and doctor surfaces it as 정상 via
# CRON_KNOWN_UNMANAGED_MARKERS). Suggested crontab line on fleet nodes with
# SSH reach to gwakga:
#
#   30 */12 * * * /opt/ccc-node/scripts/honcho-peer-staleness-canary.sh >> $HOME/.ccc-node/logs/honcho-staleness.log 2>&1  # ccc-node:honcho-staleness-canary
set -uo pipefail

max_age_days="${CCC_HONCHO_STALENESS_MAX_AGE_DAYS:-14}"
workspace="${CCC_HONCHO_STALENESS_WORKSPACE:-seoyoon-family}"
level="${CCC_HONCHO_STALENESS_LEVEL:-explicit}"
exempt_peers="${CCC_HONCHO_STALENESS_EXEMPT_PEERS:-family-assistant}"
psql_cmd="${CCC_HONCHO_STALENESS_PSQL_CMD:-ssh -o ConnectTimeout=10 -o BatchMode=yes gwakga docker exec -i honcho-database-1 psql -U postgres -P pager=off -tA}"

case "$max_age_days" in
  ''|*[!0-9]*) printf 'honcho-staleness-canary: invalid max age: %s\n' "$max_age_days" >&2; exit 2 ;;
esac

query="select observed, max(created_at)::date from documents where deleted_at is null and workspace_name = '${workspace}' and level = '${level}' group by observed order by observed;"

rows=""
# transport is a simple argv list by contract
# shellcheck disable=SC2086
if ! rows="$(printf '%s\n' "$query" | $psql_cmd 2>/dev/null)"; then
  printf 'honcho-staleness-canary: cannot reach central honcho database\n' >&2
  exit 2
fi

now_epoch="$(date +%s)"
stale=0
checked=0
while IFS='|' read -r peer day; do
  [ -n "$peer" ] || continue
  case "$day" in
    [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]) ;;
    *) printf 'honcho-staleness-canary: malformed row skipped (peer redacted)\n' >&2; continue ;;
  esac
  # space-separated allowlist by contract
  # shellcheck disable=SC2086
  for exempt in $exempt_peers; do
    [ "$peer" = "$exempt" ] && continue 2
  done
  checked=$((checked + 1))
  doc_epoch="$(python3 -c "import calendar,time;print(calendar.timegm(time.strptime('$day','%Y-%m-%d')))" 2>/dev/null)" || {
    printf 'honcho-staleness-canary: unparseable date for a tracked peer\n' >&2
    exit 2
  }
  age_days=$(( (now_epoch - doc_epoch) / 86400 ))
  if [ "$age_days" -gt "$max_age_days" ]; then
    stale=$((stale + 1))
    printf 'STALE peer=%s last_document=%s age_days=%d (limit=%d)\n' "$peer" "$day" "$age_days" "$max_age_days"
  fi
done < <(printf '%s\n' "$rows")

printf 'honcho-staleness-canary: checked=%d stale=%d max_age_days=%s workspace=%s level=%s\n' \
  "$checked" "$stale" "$max_age_days" "$workspace" "$level"

[ "$stale" -eq 0 ]
