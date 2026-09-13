---
name: distill
description: Manually trigger / inspect / toggle the Session Distiller (TM-1058) — the PreCompact+SessionEnd memory pipeline that distills transcripts via Haiku and routes to Honcho + wiki-candidates. Use when the operator says `/distill`, asks to "run distill now", wants to see what the last distill captured, wants aggregate distill health stats, wants to flip between LIVE and DRY-RUN, or wants to turn distill off. Arg options are (empty)/`manual` (fire now), `status` (show last result + queue), `stats [days]` (read-only log summary), `dryrun` (enable dry-run mode), `live` (disable dry-run), `disable` (off-switch), `enable` (clear off-switch), `compact` (retroactive de-dup of pending wiki-candidates backlog).
---

# distill — Session Distiller manual control

Wraps `~/.claude/hooks/distill.sh` with a single operator-facing UX. Design: see Wiki `pages/team/<design-node>/DECISIONS.md` [TM-1058] and runbook [ND-1059..1061].

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
   > When it stops or gates distill it appends one body-free line to the shared
   > fleet ledger `~/.claude/state/autonomy-ledger.jsonl` (`{ts, layer, state,
   > detail}`, owner-only) alongside skill-autosave/autoinstall — one place to
   > see everything the switch blocked.

2. **Dispatch on `$ARGUMENTS`**:

   - **`status`** — no mutation. Resolve the installed directory containing
     this `SKILL.md` and set `DISTILL_SKILL_DIR` to that trusted path. The
     script honors `CCC_STATE_DIR` and reports the last result/log lines and
     queue totals (`pending/stale/hot`), without firing the distiller:
     ```bash
     bash "$DISTILL_SKILL_DIR/scripts/distill-status.sh"
     ```

   - **`stats [days]`** — read-only aggregate over `distill.log` (default 7 days).
     Parse the requested day count as decimal digits; use 7 for an absent or
     malformed value. Pass the validated count as a separate literal argument
     to the packaged script. Never insert raw `$ARGUMENTS` into shell source
     or use `eval`. The example below requests 14 days:
     ```bash
     bash "$DISTILL_SKILL_DIR/scripts/distill-stats.sh" stats 14
     ```
     The helper also accepts `stats`, `stats days=14`, or `14` as positional
     arguments. An environment variable named `ARGUMENTS` is not needed.
     Functional fixtures live in `tests/test_distill_skill_parsing.py`.

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

   - **`compact`** — run the queue maintenance command shown before
     **Procedure** and report its before/after sizes and summary. It rewrites
     the queue, retaining the newest PENDING entry per `title_hash` bucket,
     and refreshes `.seen`; it has no dry-run flag. Run it when the operator
     requests compact or authorizes maintenance that includes compaction.

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
- Re-enable by `mv`-ing `distill.disabled` / `distill.dryrun` to a timestamped archive name rather than deleting them, so the previous toggle state stays recoverable and the change is auditable. Choose `mv` for that reason — **not** to avoid the guard: if the guard blocks an action you believe is correct, stop and get approval instead of reaching for a verb it does not cover.
- The foreground entry in `claude/hooks/distill.sh` reads `transcript_path` and cwd/workspace metadata from hook JSON on stdin. When the path is absent or is not a file, it looks for the newest `*.jsonl` in the current `PWD`'s encoded project directory under `CLAUDE_PROJECTS_DIR` (default `~/.claude/projects`). An ordinary manual invocation with empty stdin uses that fallback; no match logs `skip reason=no-transcript`.
- Before foreground extraction is enqueued, `scope_allows_project` checks the selected transcript's parent-directory name and supplied or derived cwd against `CCC_DISTILL_SCOPE_CWDS` and the state directory's `distill.scope`. Any matching cwd, project name, or encoded scope path permits it; a nonempty scope with no match logs `skip reason=cwd-out-of-scope`. This is a metadata-based scope filter, not filesystem access control or proof of transcript ownership.
- `CLAUDE_DISTILL_TRANSCRIPT` is exported after foreground selection for the detached pipeline; it is not a foreground selection override. The internal background re-entry consumes exported pipeline inputs and does not repeat foreground selection. Do not invoke that internal entry as a way to select another session or bypass the scope filter.
- All extract output is redacted before any external send. Even so, never paste raw secrets into prompt content that feeds the transcript extraction — the distiller will see them.

## Re-verifying the pinned values

Every default and flag named above lives in the hook scripts, not here. This
file can drift from them silently, so check rather than trust it (#1630):

| Pinned here | Check |
|---|---|
| `CCC_DISTILL_MAX_WIKI_CANDS` default 3 | `grep -rn 'CCC_DISTILL_MAX_WIKI_CANDS' claude/hooks/` |
| `CCC_DISTILL_SEEN_TTL_DAYS` default 7 | `grep -rn 'CCC_DISTILL_SEEN_TTL_DAYS' claude/hooks/` |
| `CCC_DISTILL_HOTNESS_THRESHOLD` default 3 | `grep -rn 'CCC_DISTILL_HOTNESS_THRESHOLD' claude/hooks/` |
| `wiki-queue.sh --compact` exists | `grep -n -- '--compact' claude/hooks/distill/wiki-queue.sh` |
| Foreground selection does not read `CLAUDE_DISTILL_TRANSCRIPT` | `grep -n 'CLAUDE_DISTILL_TRANSCRIPT' claude/hooks/distill.sh` — export after foreground selection; detached-pipeline input is separate |
| Foreground scope check and rejection | `grep -n 'scope_allows_project\|cwd-out-of-scope' claude/hooks/distill.sh` |
| `CLAUDE_DISTILL_TIMEOUT` | `grep -n 'CLAUDE_DISTILL_TIMEOUT' claude/hooks/distill/extract.sh` — the **default is 90**; the `240` named above is a suggested raise on timeout, not the default |

If a check disagrees, the hook script is authoritative — fix this file.
