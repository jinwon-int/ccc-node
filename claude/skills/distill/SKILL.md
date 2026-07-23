---
name: distill
description: Manually trigger / inspect / toggle the Session Distiller (TM-1058) — the PreCompact+SessionEnd memory pipeline that distills transcripts via Haiku and routes to Honcho + wiki-candidates. Use when the operator says `/distill`, asks to "run distill now", wants to see what the last distill captured, wants aggregate distill health stats, wants to flip between LIVE and DRY-RUN, or wants to turn distill off. Arg is one of: (empty)/`manual` (fire now), `status` (show last result + queue), `stats [days]` (read-only log summary), `dryrun` (enable dry-run mode), `live` (disable dry-run), `disable` (off-switch), `enable` (clear off-switch), `compact` (retroactive de-dup of pending wiki-candidates backlog).
---

# distill — Session Distiller manual control

Wraps `~/.claude/hooks/distill.sh` with a single operator-facing UX. Design: see Wiki `pages/team/dungae/DECISIONS.md` [TM-1058] and runbook [ND-1059..1061].

## Modes

| arg | behavior |
|---|---|
| (empty) / `manual` | Fire distill.sh on the current session, wait for the bg pipeline, summarize what was distilled. |
| `status` | Show toggle state, last `distill-last.json`, last 5 log lines, wiki-candidates queue size. No fire. |
| `stats [days]` | Read-only aggregate summary from `distill.log` for the last N days (default 7). |
| `dryrun` | Enable DRY-RUN (extract only; no Honcho/Wiki writes). Idempotent. |
| `live` | Disable DRY-RUN. Idempotent. |
| `disable` | Off-switch on (skip everything). |
| `enable` | Off-switch off. |
| `compact` | One-shot retroactive de-dup of PENDING wiki-candidates (same `title_hash` bucket → keep newest, refresh `.seen`). Backlog cleanup for entries queued before issue-anchored hashing (issue #298). |

Operator arg: `$ARGUMENTS`

For **`compact`**, run the queue's built-in compactor and report its summary line
(kept / dropped(dup) / buckets), plus the queue size before/after:

```bash
wc -l ~/.claude/state/wiki-candidates.md
bash ~/.claude/hooks/distill/wiki-queue.sh --compact
wc -l ~/.claude/state/wiki-candidates.md
```


## Procedure

1. **Read current toggle state** (always, regardless of mode):
   ```bash
   ls -la ~/.claude/state/distill.disabled ~/.claude/state/distill.dryrun 2>&1 | grep -v "cannot access"
   ```
   Compute the effective mode: `OFF` if `distill.disabled` exists, else `DRY-RUN` if `distill.dryrun` exists, else `LIVE`.

   > **Fleet autonomy guard (#386)** sits *above* these toggles: `CCC_AUTONOMY=kill`
   > (or `~/.claude/state/autonomy.kill`) makes distill skip entirely like `OFF` —
   > no extract LLM call, no local/external write — and `CCC_AUTONOMY=dry-run` (or
   > `~/.claude/state/autonomy.dry-run`) forces `DRY-RUN` even without
   > `distill.dryrun`. Honored on every entry path (foreground, bg re-entry,
   > SessionStart pending-drain). It never *relaxes* a stricter local toggle.

2. **Dispatch on `$ARGUMENTS`**:

   - **`status`** — no mutation. Read & report:
     ```bash
     STATE="${CCC_STATE_DIR:-$HOME/.claude/state}"
     QUEUE="$STATE/wiki-candidates.md"
     SEEN="$STATE/wiki-candidates.seen"
     jq -r '"trigger=\(.trigger) session=\(.session_id) at=\(.distilled_at) honcho=\(.honcho|length) wiki=\(.wiki_candidates|length)"' \
       "$STATE/distill-last.json" 2>/dev/null
     tail -5 "$STATE/distill.log" 2>/dev/null
     CUTOFF="$(date -u -d '7 days ago' +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || printf '0000-00-00T00:00:00Z')"
     awk -v cutoff="$CUTOFF" '
       function reset() { pending=0; distilled=""; hot=0; stale_marker=0 }
       function flush() {
         if (!seen) return
         total++
         if (pending) pending_n++
         if (hot) hot_n++
         if (pending && (stale_marker || (distilled != "" && distilled < cutoff))) stale_n++
       }
       BEGIN { reset() }
       /^## \[CAND-[0-9]+\]/ { flush(); seen=1; reset(); if ($0 ~ /🔥 HOT/) hot=1; if ($0 ~ /\(stale: pending review\)/) stale_marker=1; next }
       /^- status: pending/ { pending=1; next }
       /^- distilled-at: / { distilled=$3; next }
       END { flush(); printf "wiki-candidates total=%d pending=%d stale=%d hot=%d\n", total+0, pending_n+0, stale_n+0, hot_n+0 }
     ' "$QUEUE" 2>/dev/null
     awk -v th="${CCC_DISTILL_HOTNESS_THRESHOLD:-3}" 'NF >= 4 && $3 >= th {hot++} END { printf "seen-hot=%d threshold=%s\n", hot+0, th }' "$SEEN" 2>/dev/null
     ```

   - **`stats [days]`** — read-only aggregate over `distill.log` (default 7 days):
     ```bash
     set -uo pipefail
     ARG="${ARGUMENTS:-stats}"
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
         total[trigger]++
         next
       }
       /spawned bg pid=/ {
         # Bridge parent (start) PID -> bg (worker) PID so downstream lines that
         # log only the bg pid still resolve their trigger via the cache.
         if (match($0, /pid=[0-9]+/)) {
           bg=substr($0, RSTART+4, RLENGTH-4)
           # Find the most recent start-trigger by scanning known parents.
           for (p in pid_trigger) parent_trigger=pid_trigger[p]
           pid_trigger[bg]=parent_trigger
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
     ```

   - **`dryrun`** — `touch ~/.claude/state/distill.dryrun`. Confirm.

   - **`live`** — flip via rename (prefer an archiving `mv` over `rm` so the prior state stays recoverable). Use a timestamped archive:
     ```bash
     mv ~/.claude/state/distill.dryrun \
        ~/.claude/state/distill.dryrun.off-$(date -u +%Y%m%d%H%M%S) 2>&1
     ```
     If the file doesn't exist, report already LIVE.

   - **`disable`** — `touch ~/.claude/state/distill.disabled`. Confirm.

   - **`enable`** — same rename trick:
     ```bash
     mv ~/.claude/state/distill.disabled \
        ~/.claude/state/distill.disabled.off-$(date -u +%Y%m%d%H%M%S) 2>&1
     ```

   - **(empty) / `manual`** — fire & wait:
     ```bash
     bash ~/.claude/hooks/distill.sh manual
     ```
     The script returns immediately (bg detach). Poll the log up to 180 s:
     ```bash
     for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
       sleep 15
       LAST=$(tail -1 ~/.claude/state/distill.log)
       echo "[+${i}*15s] $LAST"
       case "$LAST" in
         *"done"*|*"extract failed"*|*"dry-run skipping"*|*"skip reason="*) break ;;
       esac
     done
     ```
     Then read `~/.claude/state/distill-last.json` and the last few `distill.log` lines.

3. **Report** in the structured style:
   - **Confirmed**: toggle state before/after, action taken, HTTP/exit codes if relevant.
   - **Result**: number of honcho facts pushed, number of wiki candidates queued, any new `[CAND-N]` entries.
   - **Risks/next**: if `extract failed` or timeout → suggest `CLAUDE_DISTILL_TIMEOUT=240` or smaller `MAX_TURNS`. If wiki queue grew → suggest reviewing via `/wiki-record`.

## Safety
- Scope control: by default distill accepts every transcript visible to the node. To restrict a multi-tenant node, set `CCC_DISTILL_SCOPE_CWDS` to a comma/colon-separated allowlist of cwd paths, or write one cwd/project-encoded entry per line to `~/.claude/state/distill.scope`. Out-of-scope transcripts log `skip reason=cwd-out-of-scope` and do not extract, push, or queue.
- Noise controls (issue #298): wiki-candidates are extracted only when reusable + new + settled (exclusion list in the extract prompt), capped at `CCC_DISTILL_MAX_WIKI_CANDS` (default 3) per session by wiki-queue, and deduped by topic for `CCC_DISTILL_SEEN_TTL_DAYS` (default 7). `/distill compact` cleans pre-existing duplicate backlog.
- All outputs carry provenance: `source_cwd`/`source_project` in `distill-last.json`, Honcho metadata, and wiki-candidates entries.
- DO NOT use `rm` on `distill.disabled` / `distill.dryrun` (guard blocks `rm` + system paths). Always `mv` to a timestamped archive name — same disable effect, no guard friction.
- Manual fire from inside an active Claude Code session uses **this** session's transcript. If you want to distill some **other** session, set `CLAUDE_DISTILL_TRANSCRIPT=/path/to/other.jsonl` in env before firing.
- All extract output is redacted before any external send. Even so, never paste raw secrets in the prompt that feeds the trans — the distiller will see them.
