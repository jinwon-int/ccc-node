#!/usr/bin/env bash
# Shared nunchi feed helpers (#1698) — sourced, never executed.
#
# Every feed lane writes the same ccc.nunchi.ingest.v1 liveness tick, and every
# lane can be installed on a node whose runtime provider has since moved on.
# That drift used to be invisible: the wrong lane still ran, still exited 0 and
# still wrote a fresh tick, so ccc-doctor saw a healthy lane while the fact DB
# had been frozen for weeks (gongmyoung 6 days, soonwook 43 days). The tick now
# carries the runtime provider and an explicit mismatch flag.

# Resolve the node's runtime agent provider, or '' when it cannot be determined.
#
# Process env first (an operator or test can state it outright), then the
# bridge's private .env — the same precedence bridge/start.sh uses. The value is
# deliberately NOT pinned into the cron line at install time: a frozen copy
# would agree with the lane forever and could never report the drift this
# function exists to detect.
# shellcheck disable=SC2120 # Optional fallback is used by external diagnostic callers.
nunchi_runtime_provider() {
  local value="${CCC_AGENT_PROVIDER:-}" env_file candidate
  if [ -z "$value" ]; then
    env_file="${CCC_BRIDGE_ENV_FILE:-${BOT_DATA_DIR:-${PROJECT_ROOT:-$HOME}/.telegram_bot}/.env}"
    # Diagnostics may supply the known checkout bridge/.env as a fallback.
    for candidate in "$env_file" "${1:-}"; do
      [ -z "$value" ] || break
      [ -n "$candidate" ] || continue
      if [ -f "$candidate" ] && [ ! -L "$candidate" ]; then
      # Last assignment wins, mirroring dotenv. Quotes and inline comments are
      # stripped; the file is never sourced, so no credential is expanded.
      value="$(sed -En 's/^[[:space:]]*(export[[:space:]]+)?CCC_AGENT_PROVIDER[[:space:]]*=[[:space:]]*//p' \
        "$candidate" 2>/dev/null | tail -1 | sed -e 's/[[:space:]]*#.*$//' \
        -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/" | tr -d '[:space:]')"
      fi
    done
  fi
  printf '%s' "$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]')"
}

# nunchi_write_status <status-file> <feed> <sources> <ingested> <retired> <deferred> [extra-json]
#
# Atomic (tmp + rename) so a reader never sees a half-written tick, matching the
# behaviour every lane already had. extra-json is a caller-supplied fragment of
# already-formed "key":value pairs appended verbatim, e.g. '"skipped":"..."'.
nunchi_write_status() {
  local status="$1" feed="$2" sources="$3" ingested="$4" retired="$5" deferred="$6"
  local extra="${7:-}" provider mismatch='' tmp
  provider="$(nunchi_runtime_provider)"
  # Report drift only when the runtime provider is actually known. An unknown
  # provider is not evidence of agreement, so it stays absent from the tick
  # rather than being reported as a match.
  if [ -n "$provider" ]; then
    extra="${extra:+$extra,}\"feed_provider\":\"$provider\""
    if [ "$provider" != "$feed" ]; then
      mismatch=",\"feed_provider_mismatch\":true"
      # Not "$feed-feed:" — the claude lane's script is ingest-cron.sh, so that
      # would name a file that does not exist.
      echo "nunchi $feed lane: provider drift — the installed feed is '$feed' but the runtime provider is '$provider'; this lane is ingesting from a source that no longer fills. Re-run scripts/install-nunchi.sh --apply" >&2
    fi
  fi
  tmp="$status.$$"
  if printf '{"schema":"ccc.nunchi.ingest.v1","finished_at":%d,"sources":%d,"ingested":%d,"retired":%d,"deferred":%d,"feed":"%s"%s%s}\n' \
      "$(date -u +%s)" "${sources:-0}" "${ingested:-0}" "${retired:-0}" "${deferred:-0}" \
      "$feed" "${extra:+,$extra}" "$mismatch" > "$tmp" 2>/dev/null; then
    mv -f "$tmp" "$status" 2>/dev/null || rm -f "$tmp"
  else
    rm -f "$tmp" 2>/dev/null
  fi
}
