---
description: Quick operational snapshot of this Claude Code node — git state, cron, bridge, and recent guard/audit activity.
allowed-tools: Bash(git status:*), Bash(git -C:*), Bash(crontab:*), Bash(tail:*), Bash(ls:*), Bash(systemctl:*)
---
## Live context (read-only)

- ccc-node git: !`git -C /opt/ccc-node status -sb 2>&1 | head -5`
- cron: !`crontab -l 2>&1 | grep -vE '^#|^$' | head`
- bridge status: !`/opt/ccc-node/bridge/start.sh --path /root --status 2>&1 | tail -5 || echo "(bridge status unavailable)"`
- recent audit (last 5): !`tail -5 /root/.claude/state/audit.jsonl 2>/dev/null || echo "(no audit log yet)"`
- approval-needed markers (last 5): !`tail -5 /root/.claude/state/approval-needed.log 2>/dev/null || echo "(none)"`

## Task

Summarize the node's current operational state for the operator, using the structured report format (confirmed facts / changes / risks / next). Live-check anything mutable before asserting; flag anything that looks off (uncommitted changes, missing cron, bridge down, recent denials). Keep it concise and in Korean.
