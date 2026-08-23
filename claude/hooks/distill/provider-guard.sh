#!/usr/bin/env bash
# Classify claude -p extract failures and persist a node-level cooldown.
# Sourced by extract.sh / distill.sh / pending-drain.sh. Fail-open.
#
# Auth/quota errors are not transient: retrying them on every SessionStart
# burned CPU and distill.log (yukson 2026-08-23: 1.3M pending-drain lines
# after #1248 removed the re-entry fork bomb). Cooldown stops new extract
# spawns until the window elapses; jobs stay on disk.

ccc_distill_classify_text() {
  local text="${1:-}"
  case "$text" in
    *"Not logged in"*|*"Please run /login"*) printf '%s\n' not_logged_in ;;
    *"OAuth session expired"*|*"could not be refreshed"*) printf '%s\n' oauth_expired ;;
    *"weekly limit"*|*"Weekly limit"*|*"hit your weekly limit"*) printf '%s\n' weekly_limit ;;
    *"rate limit"*|*"Rate limit"*|*"rate_limit"*) printf '%s\n' rate_limited ;;
    *) printf '%s\n' extract_failed ;;
  esac
}

ccc_distill_class_is_hard() {
  case "${1:-}" in
    not_logged_in|oauth_expired|weekly_limit|rate_limited) return 0 ;;
    *) return 1 ;;
  esac
}

ccc_distill_state_dir() {
  printf '%s' "${CCC_STATE_DIR:-${STATE_DIR:-${HOME:-/root}/.claude/state}}"
}

ccc_distill_cooldown_path() {
  printf '%s/distill.cooldown' "$(ccc_distill_state_dir)"
}

ccc_distill_last_error_path() {
  printf '%s/distill-last-error.json' "$(ccc_distill_state_dir)"
}

ccc_distill_cooldown_until_iso() {
  local cls="${1:-extract_failed}"
  local now epoch until_epoch auth_s rate_s
  now="$(date -u +%s)"
  auth_s="${CCC_DISTILL_AUTH_COOLDOWN_SEC:-21600}"
  rate_s="${CCC_DISTILL_RATE_COOLDOWN_SEC:-1800}"
  case "$auth_s" in ''|*[!0-9]*) auth_s=21600 ;; esac
  case "$rate_s" in ''|*[!0-9]*) rate_s=1800 ;; esac
  case "$cls" in
    weekly_limit)
      # Next 10:00 Asia/Seoul == 01:00 UTC. If that instant is already past,
      # roll to the following day.
      until_epoch="$(python3 -c '
import datetime
now = datetime.datetime.now(datetime.timezone.utc)
today = now.date()
candidate = datetime.datetime(today.year, today.month, today.day, 1, 0, 0, tzinfo=datetime.timezone.utc)
if candidate <= now:
    candidate = candidate + datetime.timedelta(days=1)
print(int(candidate.timestamp()))
' 2>/dev/null || echo $((now + 43200)))"
      ;;
    rate_limited) until_epoch=$((now + rate_s)) ;;
    not_logged_in|oauth_expired) until_epoch=$((now + auth_s)) ;;
    *) until_epoch=0 ;;
  esac
  [ "$until_epoch" -gt 0 ] || return 1
  date -u -d "@$until_epoch" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null \
    || python3 -c "import datetime; print(datetime.datetime.fromtimestamp($until_epoch, datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))"
}

ccc_distill_write_last_error() {
  local cls="${1:-extract_failed}" ec="${2:-1}"
  local path dir tmp
  path="$(ccc_distill_last_error_path)"
  dir="$(dirname "$path")"
  mkdir -p "$dir" 2>/dev/null || return 0
  tmp="$path.tmp.$$"
  printf '{"class":"%s","ec":%s,"at":"%s"}\n' \
    "$cls" "$ec" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$tmp" 2>/dev/null || return 0
  chmod 600 "$tmp" 2>/dev/null || true
  mv -f "$tmp" "$path" 2>/dev/null || rm -f "$tmp"
}

ccc_distill_set_cooldown() {
  local cls="${1:-}"
  local until path dir tmp
  ccc_distill_class_is_hard "$cls" || return 0
  until="$(ccc_distill_cooldown_until_iso "$cls")" || return 0
  [ -n "$until" ] || return 0
  path="$(ccc_distill_cooldown_path)"
  dir="$(dirname "$path")"
  mkdir -p "$dir" 2>/dev/null || return 0
  tmp="$path.tmp.$$"
  printf '{"class":"%s","until":"%s","at":"%s"}\n' \
    "$cls" "$until" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$tmp" 2>/dev/null || return 0
  chmod 600 "$tmp" 2>/dev/null || true
  mv -f "$tmp" "$path" 2>/dev/null || rm -f "$tmp"
}

ccc_distill_cooldown_class() {
  local path raw until now
  path="$(ccc_distill_cooldown_path)"
  [ -f "$path" ] || return 1
  raw="$(cat "$path" 2>/dev/null)" || return 1
  until="$(printf '%s' "$raw" | python3 -c 'import json,sys
try:
    d=json.load(sys.stdin)
    print(d.get("until") or "")
    print(d.get("class") or "extract_failed")
except Exception:
    raise SystemExit(1)
' 2>/dev/null)" || return 1
  now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  local until_iso cls
  until_iso="$(printf '%s\n' "$until" | sed -n '1p')"
  cls="$(printf '%s\n' "$until" | sed -n '2p')"
  [ -n "$until_iso" ] || return 1
  # Lexicographic compare works for UTC Z timestamps.
  if [ "$now" \< "$until_iso" ] || [ "$now" = "$until_iso" ]; then
    printf '%s\n' "${cls:-extract_failed}"
    return 0
  fi
  return 1
}
