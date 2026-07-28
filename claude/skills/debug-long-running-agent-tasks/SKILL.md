---
name: debug-long-running-agent-tasks
description: Diagnose and fix stalled background agents that report "running" or "came to rest" but produce no output files — verify via the filesystem, then relaunch as foreground agents with small logical chunks. Use when long-running file-producing agent tasks stall or repeat task-notifications without artifacts appearing.
category: claude
---

# debug-long-running-agent-tasks — fix stalled background agents

## When to Use
You've launched background Agent workers for long-running file-producing tasks and they report "running" or "came to rest" repeatedly, but the filesystem shows no new output files. Task-notifications fire but no artifacts appear.

## Root Cause
- Background agents + Monitor-based streaming shell = tasks yield/stall without producing output (observed pattern, not a guaranteed diagnosis)
- Single large-scope commands (e.g., "process entire year") in Monitor mode consistently stall
- Foreground agents with chunked small synchronous commands are reliable

## Procedure
1. Verify filesystem, don't trust status claims. Use Glob or Bash to check if expected output files exist:
   ```bash
   ls -la /path/to/output/ | grep -E '\.json|\.csv|expected_file'
   ```
   If nothing produced, proceed to step 2.

2. Stop stalled background agents via TaskStop [task_id].

3. Identify logical chunks: break work by subject, date range, category, or document section (not by time or iteration count).

4. Relaunch as foreground agents:
   - Set run_in_background: false in Agent tool calls
   - Each agent works on ONE logical unit only
   - Each agent makes many small synchronous tool calls (per-item, per-subject), not one large Monitor command
   - Launch multiple chunked agents in parallel in the same message to exploit task batching

## Safety
- Foreground agents block your response during execution (expect multi-minute delays)
- Always verify filesystem before trusting agent status; "came to rest" does not guarantee output
- Use when output completeness is more critical than response latency

## Verification
- Output files should appear immediately after foreground agents launch
- Heuristic from observed sessions (not a hard rule): successful workers show very many tool_uses (order of 100+); stalled workers plateau with few uses then repeat notifications without progress
- For idempotent work, spot-check that already-processed items are skipped cleanly
