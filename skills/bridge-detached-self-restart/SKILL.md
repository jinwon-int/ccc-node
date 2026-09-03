---
name: bridge-detached-self-restart
description: Restart the systemd service that hosts the CURRENT bridge/agent session (Telegram bridge, gateway, broker tunnel) without losing the post-restart verification step. A plain `systemctl restart <own-service>` kills the calling shell/session mid-command — any verification lines written after it never run, and the next session sees no evidence. Detach the restart+verify sequence into an independent `systemd-run` transient unit BEFORE issuing the restart, so it survives being severed from its own parent, then read its log/registry entry from the next session. Use whenever you are about to restart a service you are currently running inside of (config reload, timeout tuning, self-update checkout swap), not for restarting a peer/unrelated service (plain `systemctl restart` is fine there).
metadata:
  type: ccc-skill
---

# bridge-detached-self-restart

## The trap

If your current shell/agent session is a child of (or otherwise depends on)
the very systemd service you are restarting, `systemctl restart <svc>` does
not return cleanly to a script that then verifies the result — the restart
tears down your own process tree partway through, so any verification lines
written *after* the restart command in the same script/turn silently never
execute. The operator sees "restarted" with no confirmation that the new
process actually came up healthy with the new config. This is a narrower,
more dangerous case than `bridge-safe-detached-run` (which is about the
*session* dying under a long job) — here the *action itself* is what kills
the caller.

## When to Use

- Restarting the Telegram/CCC bridge service from a bridge session (e.g. after
  editing `.env` timeout/config values — see
  `bridge-timeout-performance-tuning-and-verification`)
- Restarting a gateway/broker-tunnel unit from a session that runs on top of it
- Any self-update flow that swaps the checkout and then restarts the service
  serving the current session (see `/root/.ccc-node/self-restart-verify.sh`
  for a prior art example of this exact pattern)
- **Not** for restarting an unrelated peer service you merely operate — a plain
  `systemctl restart <svc>` + inline check is fine when your own session
  doesn't run inside it.

## Procedure

1. **Write the restart+verify sequence as a single detached script**, not as
   inline commands. It must itself write its output to a persistent log path
   (not stdout) because stdout is going to the session that's about to die:
   ```bash
   cat > /tmp/self-restart-verify.sh <<'EOF'
   #!/usr/bin/env bash
   set -uo pipefail
   OUT=$HOME/.claude/state/self-restart-verify-<job>.log
   { echo "=== restart $(date -u +%FT%TZ) ==="; } > "$OUT" 2>&1
   systemctl restart <own-service>.service >> "$OUT" 2>&1
   sleep 15
   {
     systemctl show <own-service> -p ActiveState,SubState,MainPID,NRestarts
     # + any app-specific health check (health.json, curl localhost, etc.)
     echo "=== done $(date -u +%FT%TZ) ==="
   } >> "$OUT" 2>&1
   EOF
   chmod +x /tmp/self-restart-verify.sh
   ```

2. **Launch it as a `systemd-run` transient unit with `--collect`**, following
   `bridge-safe-detached-run` Step 1 — inject `HOME`/`PATH` explicitly, since
   systemd-run does not inherit the shell environment:
   ```bash
   systemd-run --unit self-restart-<job> --collect \
     --property=Environment=HOME=$HOME \
     --property=Environment=PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin \
     /tmp/self-restart-verify.sh
   ```
   This is the critical step: because the unit is owned by PID 1 (not by the
   session/service you're about to kill), it survives the restart it triggers.

3. **Register the job** so the *next* session (which will be a fresh process
   spawned by the restarted service, not a continuation of this one) can find
   the evidence:
   ```bash
   python3 ~/.claude/hooks/lib/detached_jobs.py register \
     --unit self-restart-<job> --log "$OUT" --summary "self-restart <own-service> for <reason>"
   ```

4. **End the turn / do not attempt to verify inline.** The current session is
   about to die when the restart fires; there is nothing to poll from inside
   it. Trust the registry — SessionStart on the next session sweeps it and
   surfaces `done`/`running`/`lost` with the real exit evidence.

5. **On the next session**, read the log, confirm `MainPID` changed and the
   app-specific health check passed, then `ack` the job:
   ```bash
   tail -30 "$OUT"
   python3 ~/.claude/hooks/lib/detached_jobs.py ack --unit self-restart-<job>
   ```

## Safety

- Restarting bridge/gateway/broker-tunnel infrastructure is approval-gated
  (broker-public-tunnel and auth-proxy units on broker/relay hosts — e.g.
  `<node>-broker-public-tunnel.service` — must not be stopped without explicit
  operator approval; broker/Gateway restarts require fresh approval per
  USER.md). Detaching the restart does not bypass that — get approval before
  step 2, not after.
- Never put secrets in the unit name/command line (visible in `systemctl`/journal).
- If the service does not come back (`ActiveState` not `active` after the
  sleep window, or the app health check fails), the log is the only forensic
  trail — do not assume success just because the registry shows the job as
  `done`; read the actual verification output.

## Verification

- `self-restart-<job>.service` shows in `systemd-run`'s immediate return
  ("Running as unit: ...") before the restart fires.
- The log file (not stdout — stdout died with the session) contains both a
  new `MainPID` and a passing app-specific health check.
- `detached_jobs.py list` on the next session shows the job as `done` with
  real exit evidence, not `lost`.
- Compare pre- and post-restart `MainPID`/config (e.g. `cat /proc/<pid>/environ`)
  to confirm the intended change actually took effect, per
  `bridge-timeout-performance-tuning-and-verification`.
