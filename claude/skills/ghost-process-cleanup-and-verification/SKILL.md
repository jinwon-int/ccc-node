---
name: ghost-process-cleanup-and-verification
description: "Identify, back up, and cleanly remove stale background processes (cron, timers, scheduled tasks) while verifying parallel installations aren't collateral damage."
---

## When to Use

- When a background process (cron job, systemd timer, at job, scheduled task) exists but should not (orphaned, stale, superseded, or running in a layer that should be empty)
- When the same process or task exists in multiple places (e.g., user crontab and root crontab, or redundant nodes) and only one copy should be removed
- After detecting that a ghost process is no longer serving its purpose (e.g., a sync job that ran once but should have been one-shot, or a utility that was replaced)
- As part of fleet hygiene or post-decommission cleanup

## Procedure

1. Back up the entire source configuration before any removal:
   - `crontab -l > crontab-backup-$(date +%Y%m%d-%H%M%S).txt` (for user cron)
   - `sudo crontab -l > crontab-root-backup-$(date +%Y%m%d-%H%M%S).txt` (for root cron)
   - For systemd timers: `systemctl list-timers --all > systemd-timers-backup.txt`
   - Store backups in a durable location outside the tree you are editing
     (e.g., `"${CCC_CLAUDE_DIR:-$HOME/.claude}"/backups/`)
2. Identify the exact lines or entries to remove using grep, diff, or visual inspection. Confirm which lines match the stale process and which are to be kept.
3. Remove only the targeted entries. Use precise editing:
   - `crontab -e` and delete the lines manually, or
   - `grep -v 'pattern' crontab-backup.txt | crontab -` (if safe), or
   - Use sed/awk with exact line numbers
   - Do NOT bulk-delete or replace if other installations exist in parallel
4. Verify collateral: check that the "real" / canonical installation remains untouched:
   - If removing from root cron, verify user cron is unchanged and vice versa
   - If removing from one node, verify parallel nodes still have their copies
   - Re-check file ownership and mode (should still be correct)
5. Monitor for ghost activity cessation:
   - Tail relevant log files (e.g., `/var/log/syslog`, cron logs) to confirm the ghost process is no longer being invoked
   - If the ghost ran on a fixed schedule (e.g., every 10 minutes), wait at least one full cycle (e.g., 15 minutes) and confirm no new entries
   - Check for any output files the ghost process would have created — verify they stop being updated

## Safety

- Always back up before any deletion. Test restoration from backup to ensure it works.
- If parallel installations exist (user + root, or redundant services), map them first and remove only the intended target.
- Never use `crontab -r` or `> /dev/null` redirection unless you're certain you're replacing correctly.

## Verification

- Compare `crontab -l` before and after; diff should show only the removed lines.
- Inspect the backup to confirm you can restore if needed.
- Run the process scheduler once more (cron, systemd, at) and verify the ghost job does not appear.
- If the ghost job had logging, wait one cycle and confirm no new log entries from that job.
