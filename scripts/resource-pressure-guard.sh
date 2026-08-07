#!/usr/bin/env bash
# Preserve the Termux/Tailscale control plane under Android memory pressure.
# Only provider and memory-refresh children are terminated; bridge/sshd/crond
# and the native A2A supervisor remain untouched.
#
# Currently gongyung-specific: this guard exists because that node is a
# Galaxy S20 FE phone (5.5GiB RAM) subject to Android OOM/thermal pressure
# that VPS nodes never see. It ships in the shared repo so gongyung's
# self-update can keep it current, but setup.sh does not install its cron on
# every node — enable it per node by adding the crontab line yourself where
# the same low-RAM/thermal constraints apply.
set -u
umask 077

termux_prefix="${PREFIX:-/data/data/com.termux/files/usr}"
PATH="$termux_prefix/bin:$PATH"

source_name="${1:-cron}"
watch_root="${CCC_RESOURCE_GUARD_ROOT:-$HOME/.ccc-node/device-watch}"
state_dir="$watch_root/state"
log_dir="$watch_root/logs"
state_file="$state_dir/resource-pressure-guard.json"
refresh_block="$state_dir/resource-pressure-refresh.block"
guard_log="$watch_root/resource-pressure-guard.log"
event_log="$log_dir/events.jsonl"
lock_dir="${CCC_RESOURCE_GUARD_LOCK_DIR:-$termux_prefix/tmp/resource-pressure-guard.lock}"
dry_run="${CCC_RESOURCE_GUARD_DRY_RUN:-0}"

mem_warn_kb="${CCC_RESOURCE_MEM_WARN_KB:-1500000}"
mem_critical_kb="${CCC_RESOURCE_MEM_CRITICAL_KB:-1000000}"
mem_emergency_kb="${CCC_RESOURCE_MEM_EMERGENCY_KB:-700000}"
mem_recover_kb="${CCC_RESOURCE_MEM_RECOVER_KB:-1800000}"
thermal_warn_mc="${CCC_RESOURCE_THERMAL_WARN_MC:-75000}"
thermal_critical_mc="${CCC_RESOURCE_THERMAL_CRITICAL_MC:-85000}"
thermal_recover_mc="${CCC_RESOURCE_THERMAL_RECOVER_MC:-68000}"
trip_required="${CCC_RESOURCE_TRIP_SAMPLES:-2}"
recover_required="${CCC_RESOURCE_RECOVER_SAMPLES:-3}"
provider_max_age_sec="${CCC_RESOURCE_PROVIDER_MAX_AGE_SEC:-1800}"

mkdir -p "$state_dir" "$log_dir"
chmod 700 "$watch_root" "$state_dir" "$log_dir" 2>/dev/null || true

if ! mkdir "$lock_dir" 2>/dev/null; then
  old_pid="$(sed -n '1p' "$lock_dir/pid" 2>/dev/null || true)"
  if [[ "$old_pid" =~ ^[0-9]+$ ]] && kill -0 "$old_pid" 2>/dev/null; then
    exit 0
  fi
  rm -f "$lock_dir/pid"
  rmdir "$lock_dir" 2>/dev/null || exit 0
  mkdir "$lock_dir" 2>/dev/null || exit 0
fi
printf '%s\n' "$$" >"$lock_dir/pid"
trap 'rm -f "$lock_dir/pid"; rmdir "$lock_dir" 2>/dev/null || true' EXIT INT TERM

is_uint() { [[ "${1:-}" =~ ^[0-9]+$ ]]; }
for value in "$mem_warn_kb" "$mem_critical_kb" "$mem_emergency_kb" \
  "$mem_recover_kb" "$thermal_warn_mc" "$thermal_critical_mc" \
  "$thermal_recover_mc" "$trip_required" "$recover_required" \
  "$provider_max_age_sec"; do
  is_uint "$value" || exit 2
done

mem_available_kb="${CCC_RESOURCE_SAMPLE_MEM_KB:-$(awk '/^MemAvailable:/{print $2}' /proc/meminfo 2>/dev/null)}"
thermal_max_mc="${CCC_RESOURCE_SAMPLE_THERMAL_MC:-}"
if [ -z "$thermal_max_mc" ]; then
  thermal_max_mc=-1
  for zone in /sys/class/thermal/thermal_zone*; do
    [ -r "$zone/temp" ] || continue
    zone_value="$(cat "$zone/temp" 2>/dev/null || true)"
    is_uint "$zone_value" || continue
    [ "$zone_value" -gt "$thermal_max_mc" ] && thermal_max_mc="$zone_value"
  done
fi
is_uint "$mem_available_kb" || exit 2
[[ "$thermal_max_mc" =~ ^-?[0-9]+$ ]] || exit 2

previous='{"mode":"normal","trip_count":0,"recover_count":0}'
if jq -e 'type == "object"' "$state_file" >/dev/null 2>&1; then
  previous="$(cat "$state_file")"
fi
previous_mode="$(jq -r '.mode // "normal"' <<<"$previous")"
trip_count="$(jq -r '.trip_count // 0' <<<"$previous")"
recover_count="$(jq -r '.recover_count // 0' <<<"$previous")"
is_uint "$trip_count" || trip_count=0
is_uint "$recover_count" || recover_count=0

pressure=false
severe=false
emergency=false
if [ "$mem_available_kb" -le "$mem_warn_kb" ] || [ "$thermal_max_mc" -ge "$thermal_warn_mc" ]; then
  pressure=true
fi
if [ "$mem_available_kb" -le "$mem_critical_kb" ] \
  || [ "$thermal_max_mc" -ge "$thermal_critical_mc" ] \
  || { [ "$mem_available_kb" -le "$mem_warn_kb" ] && [ "$thermal_max_mc" -ge 80000 ]; }; then
  severe=true
fi
if [ "$mem_available_kb" -le "$mem_emergency_kb" ]; then
  emergency=true
fi

provider_pids=()
refresh_pids=()
stale_provider_pids=()
current_uid="$(id -u)"
oldest_provider_age_sec=0

collect_owned_pids() {
  local pattern="$1" pid uid_line cmdline
  if [ "$dry_run" = 1 ] && [ -n "${CCC_RESOURCE_TEST_PIDS:-}" ]; then
    for pid in ${CCC_RESOURCE_TEST_PIDS}; do
      is_uint "$pid" && printf '%s\n' "$pid"
    done
    return 0
  fi
  while IFS= read -r pid; do
    is_uint "$pid" || continue
    [ "$pid" != "$$" ] || continue
    uid_line="$(awk '/^Uid:/{print $2; exit}' "/proc/$pid/status" 2>/dev/null || true)"
    [ "$uid_line" = "$current_uid" ] || continue
    cmdline="$(tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null || true)"
    [[ "$cmdline" =~ $pattern ]] || continue
    printf '%s\n' "$pid"
  done < <(pgrep -f "$pattern" 2>/dev/null || true)
}

while IFS= read -r pid; do [ -n "$pid" ] && provider_pids+=("$pid"); done \
  < <(collect_owned_pids 'claude\.exe\.real')
if ! { [ "$dry_run" = 1 ] && [ -n "${CCC_RESOURCE_TEST_PIDS:-}" ]; }; then
  while IFS= read -r pid; do [ -n "$pid" ] && refresh_pids+=("$pid"); done \
    < <(collect_owned_pids '(/\.claude/hooks/refresh-memory\.sh|ccc_memory_index\.py)')
fi

is_descendant_of() {
  local current="$1" ancestor="$2" parent steps=0
  is_uint "$current" && is_uint "$ancestor" || return 1
  while [ "$current" -gt 1 ] && [ "$steps" -lt 32 ]; do
    [ "$current" = "$ancestor" ] && return 0
    parent="$(awk '/^PPid:/{print $2; exit}' "/proc/$current/status" 2>/dev/null || true)"
    is_uint "$parent" || return 1
    current="$parent"
    steps=$((steps + 1))
  done
  [ "$current" = "$ancestor" ]
}

bridge_bot_pid="$(sed -n '1p' "$HOME/.telegram_bot/bot.pid" 2>/dev/null || true)"
for pid in "${provider_pids[@]}"; do
  if [ "$dry_run" = 1 ] && [ -n "${CCC_RESOURCE_TEST_PIDS:-}" ]; then
    provider_age_sec="${CCC_RESOURCE_TEST_PROVIDER_AGE_SEC:-0}"
    provider_is_bridge_child=true
  else
    provider_age_sec="$(ps -o etimes= -p "$pid" 2>/dev/null | tr -d '[:space:]')"
    provider_is_bridge_child=false
    if is_uint "$bridge_bot_pid" && kill -0 "$bridge_bot_pid" 2>/dev/null \
      && is_descendant_of "$pid" "$bridge_bot_pid"; then
      provider_is_bridge_child=true
    fi
  fi
  is_uint "$provider_age_sec" || provider_age_sec=0
  [ "$provider_age_sec" -gt "$oldest_provider_age_sec" ] \
    && oldest_provider_age_sec="$provider_age_sec"
  if [ "$provider_is_bridge_child" = true ] \
    && [ "$provider_age_sec" -ge "$provider_max_age_sec" ]; then
    stale_provider_pids+=("$pid")
  fi
done

mode="$previous_mode"
action="none"
terminated=0
killed=0

if [ "$severe" = true ]; then
  trip_count=$((trip_count + 1))
else
  trip_count=0
fi

if [ "$pressure" = true ]; then
  recover_count=0
  [ "$mode" = protected ] || mode=warning
  if [ "$dry_run" != 1 ]; then
    printf '%s\n' "$(date -Is)" >"$refresh_block"
    chmod 600 "$refresh_block" 2>/dev/null || true
    for pid in "${provider_pids[@]}"; do renice 10 -p "$pid" >/dev/null 2>&1 || true; done
  fi
elif [ "$mem_available_kb" -ge "$mem_recover_kb" ] && [ "$thermal_max_mc" -le "$thermal_recover_mc" ]; then
  if [ "$previous_mode" = normal ]; then
    recover_count=0
  else
    recover_count=$((recover_count + 1))
    if [ "$recover_count" -ge "$recover_required" ]; then
      mode=normal
      trip_count=0
      recover_count=0
      [ "$dry_run" = 1 ] || rm -f "$refresh_block"
      action="recovered"
    fi
  fi
fi

terminate_pids() {
  local pid still_up=() _attempt
  [ "$#" -gt 0 ] || return 0
  if [ "$dry_run" = 1 ]; then
    terminated=$((terminated + $#))
    return 0
  fi
  for pid in "$@"; do
    kill -TERM "$pid" 2>/dev/null && terminated=$((terminated + 1)) || true
  done
  for _attempt in 1 2 3 4 5; do
    still_up=()
    for pid in "$@"; do kill -0 "$pid" 2>/dev/null && still_up+=("$pid"); done
    [ "${#still_up[@]}" -eq 0 ] && return 0
    sleep 1
  done
  for pid in "${still_up[@]}"; do
    kill -KILL "$pid" 2>/dev/null && killed=$((killed + 1)) || true
  done
}

if [ "$severe" = true ] && { [ "$trip_count" -ge "$trip_required" ] || [ "$emergency" = true ]; }; then
  mode=protected
  action="terminate-provider"
  terminate_pids "${provider_pids[@]}"
  terminate_pids "${refresh_pids[@]}"
  trip_count=0
elif [ "$pressure" = true ] && [ "${#stale_provider_pids[@]}" -gt 0 ]; then
  # Only reap long-running providers while the device is ALSO actually under
  # memory/thermal pressure right now (#gongyung-aborted-streaming). Age alone
  # used to fire this unconditionally, killing healthy in-progress turns
  # (observed: xhigh-effort turns >15min with 2GB+ free, no pressure at all).
  action="terminate-stale-provider"
  terminate_pids "${stale_provider_pids[@]}"
fi

now="$(date -Is)"
jq -cn \
  --arg mode "$mode" --argjson trip_count "$trip_count" \
  --argjson recover_count "$recover_count" --arg time "$now" \
  --argjson mem_available_kb "$mem_available_kb" \
  --argjson thermal_max_mc "$thermal_max_mc" \
  --argjson oldest_provider_age_sec "$oldest_provider_age_sec" --arg action "$action" \
  '{mode:$mode,trip_count:$trip_count,recover_count:$recover_count,updated_at:$time,
    mem_available_kb:$mem_available_kb,thermal_max_mc:$thermal_max_mc,
    oldest_provider_age_sec:$oldest_provider_age_sec,last_action:$action}' \
  >"$state_file.tmp.$$"
mv "$state_file.tmp.$$" "$state_file"
chmod 600 "$state_file" 2>/dev/null || true

printf '%s source=%s mode=%s mem_kb=%s thermal_mc=%s providers=%s oldest_provider_age_sec=%s refreshers=%s action=%s term=%s kill=%s dry_run=%s\n' \
  "$now" "$source_name" "$mode" "$mem_available_kb" "$thermal_max_mc" \
  "${#provider_pids[@]}" "$oldest_provider_age_sec" "${#refresh_pids[@]}" \
  "$action" "$terminated" "$killed" "$dry_run" \
  >>"$guard_log"

if [ "$mode" != "$previous_mode" ] || [ "$action" != none ]; then
  jq -cn --arg time "$now" --arg event "resource_pressure" --arg source "$source_name" \
    --arg previous "$previous_mode" --arg mode "$mode" --arg action "$action" \
    --argjson mem_available_kb "$mem_available_kb" --argjson thermal_max_mc "$thermal_max_mc" \
    --argjson provider_count "${#provider_pids[@]}" \
    --argjson oldest_provider_age_sec "$oldest_provider_age_sec" --argjson terminated "$terminated" \
    '{time:$time,event:$event,source:$source,previous:$previous,mode:$mode,action:$action,
      mem_available_kb:$mem_available_kb,thermal_max_mc:$thermal_max_mc,
      provider_count:$provider_count,oldest_provider_age_sec:$oldest_provider_age_sec,
      terminated:$terminated}' >>"$event_log"
fi

jq -cn --arg mode "$mode" --arg action "$action" \
  --argjson mem_available_kb "$mem_available_kb" --argjson thermal_max_mc "$thermal_max_mc" \
  --argjson providers "${#provider_pids[@]}" \
  --argjson oldest_provider_age_sec "$oldest_provider_age_sec" --argjson terminated "$terminated" \
  --argjson dry_run "$dry_run" \
  '{mode:$mode,action:$action,mem_available_kb:$mem_available_kb,
    thermal_max_mc:$thermal_max_mc,providers:$providers,
    oldest_provider_age_sec:$oldest_provider_age_sec,terminated:$terminated,dry_run:$dry_run}'
