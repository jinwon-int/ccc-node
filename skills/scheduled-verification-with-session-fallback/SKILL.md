---
name: scheduled-verification-with-session-fallback
description: Schedule a verification/check to run at a future time, choosing a scheduler that survives what can happen before it fires — a persistent systemd timer/crontab when the wait can cross a reboot (any multi-hour or overnight wait), a systemd-run transient timer only for short waits — and treating session-bounded CronCreate as a last resort. Use when a check must fire at a known future time, when a scheduled check silently never ran, or before claiming a node "cannot schedule durably".
---

## When to Use

- A verification, audit, or re-check must run at a specific future time (batch window, cron audit hour, deadline).
- The check may fire after the current session ends.
- A previously scheduled check did not fire and you need to diagnose why.
- You are about to record a claim that "this node has no durable scheduler."

## Core rule (read this first)

**If `systemd-run` is available, do NOT use a session-bounded scheduler (CronCreate).**

**A `systemd-run` timer is transient: its unit lives under `/run/systemd/transient/`
and is gone after a reboot — it survives session death, not a reboot.** If the
wait can cross a reboot (unattended-upgrades `Automatic-Reboot`, kernel updates,
maintenance windows such as 03:15 KST), use a persistent unit or crontab
(tier 2). Treat any overnight or multi-hour wait as reboot-crossing unless you
have checked that no reboot is pending or allowed in the window.

Session-bounded scheduling is a fallback for nodes with *no* durable mechanism —
not a default. Choosing it while `systemd-run` exists is a silent data-loss bug:
the session ends, the job vanishes, and nothing logs the loss.

## Procedure

### 1. Probe scheduler CAPABILITY — not just existing entries

Listing existing timers/cron entries answers "is anything scheduled?", **not**
"can I schedule?" These are different questions. Answering the second with the
first is the classic failure (see Evidence).

Run all of these:

```sh
command -v systemd-run                 # capability: can I create a transient timer?
systemctl is-system-running            # must be running/degraded, not offline/chroot
systemd-run --help | grep on-calendar  # confirm --on-calendar / --on-active support
crontab -l 2>/dev/null | grep -vc '^#' # capability: is cron usable?
systemctl list-timers --all            # INVENTORY ONLY — never the capability test
```

A node with **zero** existing timers can still create timers. Zero inventory is
not zero capability.

If unsure, prove it — the probe is cheap and non-destructive:

```sh
systemd-run --unit=probe-$$ --on-active=20 /bin/echo ok
systemctl list-timers --all --no-legend | grep probe-
```

### 2. Choose the scheduler (strict priority)

| Priority | Mechanism | Use when |
|---|---|---|
| 1 | `systemd-run --on-calendar=` / `--on-active=` transient timer | Short waits that cannot cross a reboot (same window, no pending/allowed reboot before it fires). **Default only for these.** |
| 2 | Persistent systemd timer unit (`/etc/systemd/system`, `Persistent=true`) / crontab entry | The check recurs, **or the wait can cross a reboot — any multi-hour or overnight wait, any end-of-observation or hold-expiry action.** |
| 3 | agent-cron durable task | The check needs an agent session and durable registration exists. |
| 4 | Session-bounded one-shot (CronCreate) | **Only** when 1–3 are all unavailable. Must be reported to the user as best-effort. |

Before choosing tier 1 for a long wait, check what can reboot the node first:

```sh
ls /run/systemd/shutdown/scheduled 2>/dev/null && cat /run/systemd/shutdown/scheduled  # a reboot already queued
ls /var/run/reboot-required 2>/dev/null                                              # updates waiting for a reboot
grep -h 'Automatic-Reboot' /etc/apt/apt.conf.d/* 2>/dev/null                          # unattended reboot policy/time
```

An already-queued `shutdown -r` is **not** cancelled by later setting
`Unattended-Upgrade::Automatic-Reboot "false"` — that only stops new ones. If a
change must hold reboots, cancel the queued one (`shutdown -c`) and re-check,
or stop before starting.

Record which tier you selected **and why the higher tiers were rejected**, with
the command output that justified the rejection. "I didn't see any timers" is
not a justification.

### 3. Structure the scheduled check

- Read-only/observational; no config, secret, or shared-state mutation.
- Write output to a **durable file** with a timestamp, not to session stdout.
- Emit an explicit completion marker (e.g. `EXIT=0` line) so a later session can
  distinguish "ran and passed", "ran and failed", and "never ran".
- Include the task/issue ID in the output for traceability.

### 4. Record the working-state fallback ALWAYS

Even with a durable timer, write the fallback to
`$HOME/.claude/state/working-state.md`:

- Objective (what is being verified), scheduled time, scheduler tier + unit/job ID
- Expected output file path(s) and success signal
- The manual re-check checklist for the next session

This file is snapshotted at PreCompact and re-injected at PostCompact, so it
survives session death. It is the only thing that recovers a lost schedule.

### 5. Inform the user

State the tier chosen, the unit/job ID, the exact fire time, and — if tier 4 was
used — the explicit session-boundary risk. Never promise auto-resume for tier 4.

### 6. Next session: verify, then classify

- Artifacts present → parse and report.
- Artifacts absent → **the scheduler failed**. Do not re-schedule on the same
  tier. Re-probe capability (step 1), and if a higher tier was wrongly rejected,
  correct the recorded constraint before rescheduling.

## Evidence — the failure this skill encodes

2026-08-26, node `node-y`, issue #1287 verification:

- Step 1 was performed as inventory only (`list-timers` / `crontab -l` read as
  "0 entries") → concluded **"this node cannot schedule durably."**
- Session-bounded CronCreate reservation `7ab2cecc` was created for 04:56 KST.
- The session ended before 04:56. **The reservation vanished silently.** No log,
  no error, no notification.
- Recovery was possible *only* because the fallback checklist had been written to
  `working-state.md`.
- The false constraint was then written into durable memory **5+ times**
  ("`node-y` has no systemd timers/crontab, durable scheduling impossible").
- Live re-check the next session: `/usr/bin/systemd-run` present,
  `systemctl is-system-running` → `running`, **7 active timers, 23 crontab
  lines**. A transient timer was created successfully on the first attempt.

Two lessons, both encoded above: probe **capability**, not inventory; and a wrong
premise propagates into memory faster than it gets corrected.

### Second failure — a transient timer across a reboot (2026-10-13)

A 24 h production observation scheduled its T+24h snapshot and its reboot-hold
expiry as `systemd-run --on-calendar` transient timers. unattended-upgrades had
already queued a 03:15 reboot (`Reboot scheduled …`) **four hours before** the
hold file (`Automatic-Reboot "false"`) was written; the queued reboot still ran.
Both transient timers vanished with `/run`: the T+24h row was never written
(the automatic verdict came back INCOMPLETE), and the hold file outlived its
approved window because its expiry timer was gone. Lessons: a transient timer
survives session death but not a reboot; a reboot hold does not cancel a
reboot that is already queued.

## Safety

- Tier 4 (session-bounded) is best-effort and fails **silently** — never treat as guaranteed.
- Scheduled checks observe only; corrective actions wait for next-session approval.
- Distinguish "didn't fire" from "fired and failed" using the durable completion marker.
- Clean up one-off transient probe units after verifying (`systemctl stop <unit>.timer`).
- Before recording any "node cannot X" constraint in durable memory, run the
  capability probe. Wrong constraints are expensive to remove.

## Verification

1. Capability probe output captured before scheduling (all five commands).
2. Chosen tier logged with the rejection reason for each higher tier.
3. Unit/job ID + fire time confirmed via `systemctl list-timers` (tiers 1–2).
4. `working-state.md` fallback written and path reported.
5. Next session: expected artifact exists; if not, re-probe capability before rescheduling.
