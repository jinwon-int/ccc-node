#!/usr/bin/env bash
# remote-heartbeat-watch.sh — page when a remote file stops being refreshed.
#
# Watches a heartbeat file that a scheduled job on ANOTHER node rewrites on
# every successful run, and reports when it goes stale. Run it from a
# different node than the job it watches: the 2026-07-13 failure it exists for
# was a whole scheduler dying (the external GitHub PR/Issue watch on one node
# stopped for three months and nothing noticed), and a check driven by the
# same scheduler dies with it.
#
# Output contract (agent-cron command payload, notify=*-on-failure):
#   exit 0  fresh, still-stale inside the reminder window, or target
#           unreachable (reachability is the fleet bridge watch's job)
#   exit 1  NEW stale, STILL stale reminder, or RECOVERED — one line on stdout
#   exit 2  usage error
# State (one small JSON file) records the first stale time and the last page
# so a stale heartbeat pages once, then at most every --remind-hours.
set -uo pipefail

usage() {
  cat <<'EOF'
Usage: remote-heartbeat-watch.sh --target HOST --path FILE --max-age-min N \
  --state-file FILE [--label TEXT] [--remind-hours N]

Env: CCC_HEARTBEAT_SSH (ssh binary, default ssh), CCC_HEARTBEAT_NOW (test clock)
EOF
}

target="" path="" max_age="" state_file="" label="" remind_hours=6
while [ "$#" -gt 0 ]; do
  case "$1" in
    --target) target="${2:-}"; shift 2 ;;
    --path) path="${2:-}"; shift 2 ;;
    --max-age-min) max_age="${2:-}"; shift 2 ;;
    --state-file) state_file="${2:-}"; shift 2 ;;
    --label) label="${2:-}"; shift 2 ;;
    --remind-hours) remind_hours="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done
[[ "$target" =~ ^[A-Za-z0-9_.@-]+$ ]] || { echo "invalid --target" >&2; exit 2; }
[[ "$path" =~ ^[A-Za-z0-9_./~+-]+$ ]] || { echo "invalid --path (plain path characters only)" >&2; exit 2; }
[[ "$max_age" =~ ^[1-9][0-9]*$ ]] || { echo "invalid --max-age-min" >&2; exit 2; }
[[ "$remind_hours" =~ ^[0-9]+$ ]] || { echo "invalid --remind-hours" >&2; exit 2; }
[ -n "$state_file" ] || { echo "--state-file is required" >&2; exit 2; }
label="${label:-$target:$path}"
SSH="${CCC_HEARTBEAT_SSH:-ssh}"
now="${CCC_HEARTBEAT_NOW:-$(date +%s)}"

# Age is computed on the target so clock skew between nodes cannot fake
# freshness. `~/` is expanded by the remote shell; the path is validated above
# to plain characters, so it needs no further quoting.
remote="p=$path; case \$p in '~/'*) p=\$HOME/\${p#\~/} ;; esac; m=\$(stat -c %Y \"\$p\" 2>/dev/null) || { echo missing; exit 0; }; echo \$(( \$(date +%s) - m ))"
if ! out="$("$SSH" -o BatchMode=yes -o ConnectTimeout=10 "$target" "$remote" 2>/dev/null)"; then
  echo "heartbeat: $label unreachable (left to the bridge watch)" >&2
  exit 0
fi
out="$(printf '%s' "$out" | tail -n 1 | tr -d '[:space:]')"
if [ "$out" = missing ]; then
  age=-1
elif [[ "$out" =~ ^-?[0-9]+$ ]]; then
  age="$out"
else
  echo "heartbeat: $label unreadable reply" >&2
  exit 0
fi

read_state() { python3 -c 'import json,sys
try: d=json.load(open(sys.argv[1]))
except Exception: d={}
print(d.get("stale_since",0), d.get("last_page",0))' "$state_file" 2>/dev/null || echo "0 0"; }
write_state() {
  mkdir -p "$(dirname "$state_file")" 2>/dev/null
  local tmp="$state_file.tmp.$$"
  ( umask 077; printf '{"stale_since": %s, "last_page": %s}\n' "$1" "$2" > "$tmp" ) && mv -f "$tmp" "$state_file"
}
read -r stale_since last_page <<<"$(read_state)"

limit=$(( max_age * 60 ))
if [ "$age" -ge 0 ] && [ "$age" -le "$limit" ]; then
  if [ "$stale_since" != 0 ]; then
    write_state 0 0
    echo "RECOVERED heartbeat $label (age $(( age / 60 ))m ≤ ${max_age}m)"
    exit 1
  fi
  exit 0
fi

desc="age $(( age / 60 ))m > ${max_age}m"
[ "$age" -lt 0 ] && desc="heartbeat file missing"
if [ "$stale_since" = 0 ]; then
  write_state "$now" "$now"
  echo "NEW stale heartbeat $label ($desc) — the job that refreshes it is not running"
  exit 1
fi
if [ "$remind_hours" -gt 0 ] && [ $(( now - last_page )) -ge $(( remind_hours * 3600 )) ]; then
  write_state "$stale_since" "$now"
  echo "STILL stale heartbeat $label ($desc, since $(date -u -d "@$stale_since" +%Y-%m-%dT%H:%MZ 2>/dev/null || echo "$stale_since"))"
  exit 1
fi
exit 0
