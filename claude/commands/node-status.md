---
description: Quick operational snapshot of this Claude Code node — git state, cron, bridge, and recent guard/audit activity.
allowed-tools: Bash(git status:*), Bash(git -C:*), Bash(crontab:*), Bash(tail:*), Bash(ls:*), Bash(systemctl:*), Bash(command:*), Bash(readlink:*), Bash(grep:*), Bash(stat:*), Bash(bash /opt/ccc-node/bridge/start.sh --path /root --status:*), Bash(bash /opt/ccc-node/scripts/ccc-doctor.sh --always-on-summary:*), Bash(python3 /root/.claude/hooks/ccc-context-stats.py --one-line:*)
---
## Live context (read-only)

- ccc-node git: !`git -C /opt/ccc-node status -sb 2>&1 | head -5`
- cron: !`crontab -l 2>&1 | grep -vE '^#|^$' | head`
- bridge status: !`bash /opt/ccc-node/bridge/start.sh --path /root --status 2>&1 | tail -5 || echo "(bridge status unavailable)"`
- claude CLI integrity: !`T=$(readlink -f "$(command -v claude 2>/dev/null)" 2>/dev/null); if [ -z "$T" ]; then echo "⚠️ claude not found on PATH"; elif grep -q CLAUDE_STUB "$T" 2>/dev/null; then echo "⚠️ STUB at $T — real CLI clobbered by a test stub (see Wiki LOG-1391); restore: npm install -g @anthropic-ai/claude-code --force"; else echo "ok ($T, $(stat -c%s "$T" 2>/dev/null)B)"; fi`
- claude login: !`F="${CLAUDE_CONFIG_DIR:-${HOME:-/root}/.claude}/.credentials.json"; if [ ! -e "$F" ]; then echo "(no .credentials.json — env-token/apiKeyHelper auth not inspected)"; elif grep -Eq '"accessToken" *: *""' "$F" 2>/dev/null; then echo "⚠️ accessToken empty (expiresAt=0) — login wiped after a failed refresh; every Claude call fails (ccc-node#2397); re-auth: claude setup-token (Wiki TM-3899)"; else echo "ok (accessToken present; expiry: run ccc-doctor)"; fi`
- always-on budget: !`bash /opt/ccc-node/scripts/ccc-doctor.sh --always-on-summary 2>&1 | head -1 || echo "(always-on budget unavailable)"`
- context cost (14d): !`python3 /root/.claude/hooks/ccc-context-stats.py --one-line 2>&1 | head -1 || echo "(context stats unavailable)"`
- recent audit (last 5): !`tail -5 ${CCC_STATE_DIR:-${HOME:-/root}/.claude/state}/audit.jsonl 2>/dev/null || echo "(no audit log yet)"`
- approval-needed markers (last 5): !`tail -5 ${CCC_STATE_DIR:-${HOME:-/root}/.claude/state}/approval-needed.log 2>/dev/null || echo "(none)"`

## Task

Summarize the node's current operational state for the operator, using the structured report format (confirmed facts / changes / risks / next). Live-check anything mutable before asserting; flag anything that looks off (uncommitted changes, missing cron, bridge down, a `⚠️ STUB`/`not found` claude CLI integrity line, an `always-on: OVER` budget line, recent denials). The context-cost line is a 14-day aggregate (no bodies, basenames only) — report it as a measurement, not a fault; a high `>20k` share or many re-Reads points at the render-preview rules in `tools-cheatsheet.md` (#2262). A stubbed claude CLI silently breaks the bridge's auth probe (Claude degraded) — treat it as high priority and point to the restore command above. A `⚠️ accessToken empty` claude login line means the host login was wiped and every bridge turn/cron call is failing while units stay active — also high priority; never print the file, only point to the re-auth procedure. Keep it concise and in Korean.
