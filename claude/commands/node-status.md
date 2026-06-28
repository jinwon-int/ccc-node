---
description: Quick operational snapshot of this Claude Code node — git state, cron, bridge, and recent guard/audit activity.
allowed-tools: Bash(git status:*), Bash(git -C:*), Bash(crontab:*), Bash(tail:*), Bash(ls:*), Bash(systemctl:*), Bash(command:*), Bash(readlink:*), Bash(grep:*), Bash(stat:*)
---
## Live context (read-only)

- ccc-node git: !`git -C /opt/ccc-node status -sb 2>&1 | head -5`
- cron: !`crontab -l 2>&1 | grep -vE '^#|^$' | head`
- bridge status: !`/opt/ccc-node/bridge/start.sh --path /root --status 2>&1 | tail -5 || echo "(bridge status unavailable)"`
- claude CLI integrity: !`T=$(readlink -f "$(command -v claude 2>/dev/null)" 2>/dev/null); if [ -z "$T" ]; then echo "⚠️ claude not found on PATH"; elif grep -q CLAUDE_STUB "$T" 2>/dev/null; then echo "⚠️ STUB at $T — real CLI clobbered by a test stub (see Wiki LOG-1391); restore: npm install -g @anthropic-ai/claude-code --force"; else echo "ok ($T, $(stat -c%s "$T" 2>/dev/null)B)"; fi`
- recent audit (last 5): !`tail -5 /root/.claude/state/audit.jsonl 2>/dev/null || echo "(no audit log yet)"`
- approval-needed markers (last 5): !`tail -5 /root/.claude/state/approval-needed.log 2>/dev/null || echo "(none)"`

## Task

Summarize the node's current operational state for the operator, using the structured report format (confirmed facts / changes / risks / next). Live-check anything mutable before asserting; flag anything that looks off (uncommitted changes, missing cron, bridge down, a `⚠️ STUB`/`not found` claude CLI integrity line, recent denials). A stubbed claude CLI silently breaks the bridge's auth probe (Claude degraded) — treat it as high priority and point to the restore command above. Keep it concise and in Korean.
