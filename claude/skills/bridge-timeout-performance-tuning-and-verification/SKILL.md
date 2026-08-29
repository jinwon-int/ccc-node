---
name: bridge-timeout-performance-tuning-and-verification
description: Diagnose and safely adjust service timeout configuration for performance-limited workloads; includes backup, restart, and post-restart verification that config loaded Use when a service or bridge consistently times out during resource-intensive operations and the timeout looks like a hard limit rather than a genuine hang or deadlock.
metadata:
  type: ccc-skill
---

## When to Use
When a service or bridge consistently times out during resource-intensive operations (large payloads, repeated gate invocations, heavy background tasks), and the timeout appears to be a hard limit rather than a genuine hang or deadlock.

## Procedure

1. **Locate timeout configuration** — Find the service's environment configuration (.env, systemd unit, or config file) and grep for timeout variables. Note any historical comments on past tuning.

2. **Assess workload and timeout adequacy** — Review the service's operational history. Classify the node by workload (lightweight/standard/heavy). Determine if the current timeout is insufficient for typical operation patterns.

3. **Back up configuration** — Create a timestamped backup (e.g., `.env.bak-timeout-<newvalue>-<date>`) before making changes. Verify backup is readable.

4. **Verify restart safety** — Check the service manifest to confirm it supports graceful restart without data loss. Ensure no locks or persistent semaphores will block restart.

5. **Apply configuration change** — Edit the timeout value. Add a comment justifying the new value (e.g., "Increased to N hours for <workload class> heavy operations"). Do not apply without backup or approval in production.

6. **Restart service** — Use the native restart mechanism (systemd, etc.). Record PID before and after to confirm restart completed.

7. **Verify configuration loaded** — Inspect the new process's environment variables (e.g., `cat /proc/<new-pid>/environ`). Confirm the timeout setting now reflects the new value. Monitor service for ≥1 operational cycle.

## Safety
- Always back up before production configuration changes.
- Verify the service supports restart without data/message loss.
- Configuration alone does not propagate to running processes; restart is required to load the new value.
- In clustered services, coordinate restarts to maintain quorum/availability.

## Verification
- PID change confirms restart occurred.
- Environment variable inspection confirms new value is loaded into the process.
- Monitor for timeout events over 1–2 operational cycles to confirm the increase is adequate.
- If timeouts persist despite the increase, investigate whether the true issue is a genuine hang (not just a timeout limit); escalate to `debug-long-running-agent-tasks` if needed.
