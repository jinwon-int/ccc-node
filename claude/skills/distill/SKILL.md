---
name: distill
description: Manually trigger / inspect / toggle the Session Distiller (TM-1058) — the PreCompact+SessionEnd memory pipeline that distills transcripts via Haiku and routes to Honcho + wiki-candidates. Use when the operator says `/distill`, asks to "run distill now", wants to see what the last distill captured, wants to flip between LIVE and DRY-RUN, or wants to turn distill off. Arg is one of: (empty)/`manual` (fire now), `status` (show last result + queue), `dryrun` (enable dry-run mode), `live` (disable dry-run), `disable` (off-switch), `enable` (clear off-switch).
---

# distill — Session Distiller manual control

Wraps `~/.claude/hooks/distill.sh` with a single operator-facing UX. Design: see Wiki `pages/team/dungae/DECISIONS.md` [TM-1058] and runbook [ND-1059..1061].

## Modes

| arg | behavior |
|---|---|
| (empty) / `manual` | Fire distill.sh on the current session, wait for the bg pipeline, summarize what was distilled. |
| `status` | Show toggle state, last `distill-last.json`, last 5 log lines, wiki-candidates queue size. No fire. |
| `dryrun` | Enable DRY-RUN (extract only; no Honcho/Wiki writes). Idempotent. |
| `live` | Disable DRY-RUN. Idempotent. |
| `disable` | Off-switch on (skip everything). |
| `enable` | Off-switch off. |

Operator arg: `$ARGUMENTS`

## Procedure

1. **Read current toggle state** (always, regardless of mode):
   ```bash
   ls -la ~/.claude/state/distill.disabled ~/.claude/state/distill.dryrun 2>&1 | grep -v "cannot access"
   ```
   Compute the effective mode: `OFF` if `distill.disabled` exists, else `DRY-RUN` if `distill.dryrun` exists, else `LIVE`.

2. **Dispatch on `$ARGUMENTS`**:

   - **`status`** — no mutation. Read & report:
     ```bash
     jq -r '"trigger=\(.trigger) session=\(.session_id) at=\(.distilled_at) honcho=\(.honcho|length) wiki=\(.wiki_candidates|length)"' \
       ~/.claude/state/distill-last.json 2>/dev/null
     tail -5 ~/.claude/state/distill.log 2>/dev/null
     grep -c "^## \[CAND-" ~/.claude/state/wiki-candidates.md 2>/dev/null
     ```

   - **`dryrun`** — `touch ~/.claude/state/distill.dryrun`. Confirm.

   - **`live`** — flip via rename (NOT `rm` — `guard.sh` blocks `rm-catastrophic`). Use a timestamped archive:
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
- All outputs carry provenance: `source_cwd`/`source_project` in `distill-last.json`, Honcho metadata, and wiki-candidates entries.
- DO NOT use `rm` on `distill.disabled` / `distill.dryrun` (guard blocks `rm` + system paths). Always `mv` to a timestamped archive name — same disable effect, no guard friction.
- Manual fire from inside an active Claude Code session uses **this** session's transcript. If you want to distill some **other** session, set `CLAUDE_DISTILL_TRANSCRIPT=/path/to/other.jsonl` in env before firing.
- All extract output is redacted before any external send. Even so, never paste raw secrets in the prompt that feeds the trans — the distiller will see them.
