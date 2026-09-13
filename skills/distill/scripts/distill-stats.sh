#!/usr/bin/env bash
# /distill stats — read-only aggregate over distill.log. Packaged from the
# former inline block in skills/distill/SKILL.md so the parsing is
# deterministic and regression-tested (tests/test_distill_skill_parsing.py)
# instead of drifting inside the skill text. Behavior, including the
# scan-order last_trigger bridge and the fixed trigger row order, is
# preserved verbatim from the repaired inline awk (#1630).
#
# Read-only: every input below is opened for reading only; distill state is
# never written, renamed, or created.
#
# Usage: distill-stats.sh [words...] — accepts the same forms the
# slash-command substituted inline: (empty), `stats`, `stats <days>`,
# `stats days=<days>`, `<days>`. The window defaults to 7 days.
set -uo pipefail
# Positional arguments only; do not evaluate shell source from skill input.
ARG="$*"
[ -n "$ARG" ] || ARG="stats"
DAYS="$(printf '%s' "$ARG" | sed -E 's/^stats[[:space:]]*//; s/^days=//')"
case "$DAYS" in ''|*[!0-9]*) DAYS=7 ;; esac
LOG="${CCC_STATE_DIR:-$HOME/.claude/state}/distill.log"
CUTOFF="$(date -u -d "$DAYS days ago" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || printf '0000-00-00T00:00:00Z')"
printf '[distill stats — last %s days]\n' "$DAYS"
awk -v cutoff="$CUTOFF" '
  # Resolve trigger for a log line:
  #   1) inline `trigger=…` (preferred — current distill.sh emits it on every line)
  #   2) PID lookup against the most recent `start trigger=X pid=Y` line for the same PID
  #      (handles format drift between start/start-bg/done where PIDs differ but still
  #       lets us correlate when one stage has it and another does not)
  #   3) "unknown" (truly historical lines from older distill.sh versions)
  function get_trigger(line,    p) {
    if (match(line, /trigger=[^ ]+/)) return substr(line, RSTART+8, RLENGTH-8)
    if (match(line, /pid=[0-9]+/)) {
      p=substr(line, RSTART+4, RLENGTH-4)
      if (p in pid_trigger) return pid_trigger[p]
    }
    return "unknown"
  }
  $1 < cutoff { next }
  /start trigger=/ {
    trigger="unknown"; pid=""
    if (match($0, /trigger=[^ ]+/)) { trigger=substr($0, RSTART+8, RLENGTH-8) }
    if (match($0, /pid=[0-9]+/))    { pid=substr($0, RSTART+4, RLENGTH-4) }
    if (pid != "") pid_trigger[pid]=trigger
    last_trigger=trigger   # scan-order "most recent start", used by `spawned bg`
    total[trigger]++
    next
  }
  /spawned bg pid=/ {
    # Bridge parent (start) PID -> bg (worker) PID so downstream lines that
    # log only the bg pid still resolve their trigger via the cache.
    if (match($0, /pid=[0-9]+/)) {
      bg=substr($0, RSTART+4, RLENGTH-4)
      # Use the trigger from the most recent `start` line, tracked in
      # scan order. `for (p in pid_trigger)` was used here to mean "most
      # recent", but awk array traversal order is unspecified, so it
      # picked an arbitrary parent and mislabelled bg lines (#1630).
      if (last_trigger != "") pid_trigger[bg]=last_trigger
    }
    next
  }
  / done trigger=/ {
    trigger=get_trigger($0); elapsed=""
    if (match($0, /elapsed_s=[0-9]+/)) { elapsed=substr($0, RSTART+10, RLENGTH-10); elapsed_sum[trigger]+=elapsed; elapsed_n[trigger]++ }
    done[trigger]++
    next
  }
  /extract failed/ {
    trigger=get_trigger($0); elapsed=""
    if (match($0, /elapsed_s=[0-9]+/)) { elapsed=substr($0, RSTART+10, RLENGTH-10); elapsed_sum[trigger]+=elapsed; elapsed_n[trigger]++ }
    failed[trigger]++
    next
  }
  /dry-run skipping/ {
    trigger=get_trigger($0); elapsed=""
    if (match($0, /elapsed_s=[0-9]+/)) { elapsed=substr($0, RSTART+10, RLENGTH-10); elapsed_sum[trigger]+=elapsed; elapsed_n[trigger]++ }
    dryrun[trigger]++
    next
  }
  /skip reason=|skipped reason=/ {
    trigger=get_trigger($0)
    skip[trigger]++
    next
  }
  END {
    split("manual precompact sessionend unknown", order, " ")
    for (i=1; i<=length(order); i++) {
      t=order[i]
      if ((total[t]+done[t]+failed[t]+dryrun[t]+skip[t]) == 0) continue
      avg="-"
      if (elapsed_n[t] > 0) avg=sprintf("%ds", elapsed_sum[t]/elapsed_n[t])
      printf "%-10s %4d runs (%3d done / %3d failed / %3d dryrun / %3d skipped) avg=%s\n", t ":", total[t], done[t], failed[t], dryrun[t], skip[t], avg
    }
  }
' "$LOG" 2>/dev/null
printf '\nHoncho push: %s ok / %s queued\n' \
  "$(awk -v cutoff="$CUTOFF" '$1>=cutoff && /honcho push ok/ {n++} END{print n+0}' "$LOG" 2>/dev/null)" \
  "$(awk -v cutoff="$CUTOFF" '$1>=cutoff && (/honcho-push non-zero|honcho push failed/) {n++} END{print n+0}' "$LOG" 2>/dev/null)"
printf 'Wiki queue:   %s candidates added / %s dedup-skipped\n' \
  "$(awk -v cutoff="$CUTOFF" '$1>=cutoff && /wiki-queue session=/ {if (match($0,/added=[0-9]+/)) {n+=substr($0,RSTART+6,RLENGTH-6)}} END{print n+0}' "$LOG" 2>/dev/null)" \
  "$(awk -v cutoff="$CUTOFF" '$1>=cutoff && /wiki-queue session=/ {if (match($0,/skipped\(dup\)=[0-9]+/)) {n+=substr($0,RSTART+13,RLENGTH-13)}} END{print n+0}' "$LOG" 2>/dev/null)"
