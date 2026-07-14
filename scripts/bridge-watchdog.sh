#!/usr/bin/env bash
# crond-driven watchdog: restart ccc-node Telegram bridge if down.
# Same pattern as a2a-worker-watchdog.sh (ND-172 lesson).
#
# Debounce (incident 2026-07-08, daegyo node): a five-minute watchdog tick
# raced a concurrent manual restart (`start.sh --stop` immediately followed
# by `start.sh --daemon`). The tick landed a few seconds after the manual
# restart and read bot.pid as stale/missing before the fresh instance had
# settled, so it launched a SECOND `start.sh --daemon` on top of the
# already-restarting one. Both instances then polled Telegram with the
# same bot token; Telegram's getUpdates conflict-killed them repeatedly
# (`telegram.error.Conflict: terminated by other getUpdates request`) for
# ~6 minutes, during which in-flight Claude responses were cut off
# mid-turn (perceived as "premature session end" even though the
# underlying session_id survived in sessions.json).
#
# Fix: skip this tick if bot.pid was written very recently. A genuinely
# stale PID file (real crash) is old by definition; a PID file from an
# instance that just started is fresh. So this can only suppress a false
# "down" reading racing a fresh restart -- it never masks a real outage,
# since the next tick (GRACE_SECONDS later) will still see it if the new
# instance also failed to come up.
set -uo pipefail
# Paths and tunables are overridable for testing / non-standard installs; the
# defaults reproduce the production layout exactly (behavior-neutral).
LOG="${BRIDGE_WATCHDOG_LOG:-$HOME/.hermes/logs/bridge-watchdog.log}"
mkdir -p "$(dirname "$LOG")"
ts() { date '+%Y-%m-%d %H:%M:%S%z'; }

PID_FILE="${BRIDGE_WATCHDOG_PID_FILE:-$HOME/.telegram_bot/bot.pid}"
START="${BRIDGE_WATCHDOG_START:-$HOME/ccc-node/bridge/start.sh}"
GRACE_SECONDS="${BRIDGE_WATCHDOG_GRACE_SECONDS:-90}"
# Process-match fallback pattern (overridable so tests do not match a real
# bridge running on the same host).
PROCESS_MATCH="${BRIDGE_WATCHDOG_PROCESS_MATCH:-python -m telegram_bot}"

# Alive check: bot.pid points at a live python -m telegram_bot process
if [ -f "$PID_FILE" ]; then
  pid="$(cat "$PID_FILE" 2>/dev/null)"
  if [ -n "${pid:-}" ] && kill -0 "$pid" 2>/dev/null; then
    exit 0
  fi
fi
# Fallback: process match (covers stale/missing pid file)
if pgrep -f "$PROCESS_MATCH" >/dev/null 2>&1; then
  exit 0
fi

# Debounce: if bot.pid was touched very recently, a restart (manual or
# supervisor-driven) is very likely already in flight -- skip this tick
# instead of racing it with another `start.sh --daemon`.
if [ -f "$PID_FILE" ]; then
  now="$(date +%s)"
  mtime="$(stat -c %Y "$PID_FILE" 2>/dev/null || echo 0)"
  age=$(( now - mtime ))
  if [ "$age" -ge 0 ] && [ "$age" -lt "$GRACE_SECONDS" ]; then
    echo "[$(ts)] bridge watchdog: bot.pid is only ${age}s old (< ${GRACE_SECONDS}s grace) -- skipping this tick to avoid racing a concurrent restart" >> "$LOG"
    exit 0
  fi
fi

echo "[$(ts)] bridge watchdog: bridge down, restarting via start.sh --daemon" >> "$LOG"

if command -v termux-wake-lock >/dev/null 2>&1; then
  termux-wake-lock >/dev/null 2>&1 || true
fi

if [ -x "$START" ]; then
  bash "$START" --path "$HOME" --daemon >> "$LOG" 2>&1
  echo "[$(ts)] bridge watchdog: start.sh exit=$?" >> "$LOG"
else
  echo "[$(ts)] bridge watchdog: $START not found/executable" >> "$LOG"
fi
