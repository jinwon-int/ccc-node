---
name: bridge-safe-detached-run
description: Run a long-running command as a detached systemd transient unit so it survives Telegram-bridge/session restarts — with persistent log file, EXIT marker, HOME/PATH env injection, and a polling watcher. Use when a task may outlive the current session (installs, mining/indexing, builds, migrations, long tests), when a previous background task was killed by a bridge restart (ccc-node #822), or when systemd-run output mysteriously fails due to missing HOME.
---

# bridge-safe-detached-run

Session/bridge restarts kill ordinary background children (`&`, harness bg
tasks — ccc-node #822). For any command that may outlive the session, run it as
a **systemd transient unit** owned by PID 1, with a persistent log and an EXIT
marker, then poll the log. Formalizes the #822 workaround.

## When to Use

- Long installs / package builds / model or corpus mining / migrations / long test suites
- Any command expected to run >2–3 minutes while the session or Telegram bridge might restart
- Re-running a job that previously died with the session ("bg task killed")

## Step 1 — Launch as a transient unit

```bash
LOG=$HOME/.claude/state/<job>.log   # any persistent path; NOT /tmp if /tmp is tight
systemd-run --unit <job> --collect \
  --property=Environment=HOME=$HOME \
  --property=Environment=PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin \
  --property=WorkingDirectory=<workdir> \
  bash -c '<command> >> '"$LOG"' 2>&1; echo "EXIT=$?" >> '"$LOG"''
```

Rules:
- `--unit <job>`: pick a unique, descriptive name — collisions fail the launch
  (`reset-failed` first if re-using a name, see Step 4).
- `--collect`: the unit garbage-collects after exit (no lingering failed units to clean).
- **`HOME` injection is mandatory**: systemd-run does NOT inherit your shell env.
  Missing `HOME` breaks anything reading `~/.claude`, `~/.config`, npm, statusline
  hooks, etc. — often as silent misbehavior, not a clean error. Inject `PATH` too
  when the command needs `~/.local/bin` or non-default tool paths.
- **EXIT marker is mandatory**: `echo "EXIT=$?"` appended to the log is the only
  reliable completion signal after `--collect` removes the unit.
- Log to a persistent file (`>> log 2>&1`) — journald alone disappears with `--collect`.

## Step 2 — Watch for completion

Poll the log (survives your own session restarts too):

```bash
# quick check
tail -5 "$LOG"; grep -c '^EXIT=' "$LOG"
# blocking watcher (bounded)
for i in $(seq 1 120); do grep -q '^EXIT=' "$LOG" && break; sleep 10; done
grep '^EXIT=' "$LOG"   # EXIT=0 → success; nonzero → inspect log
```

While the unit is live: `systemctl status <job>.service` / `journalctl -u <job> -n 20`.

## Step 3 — Short synchronous variant

For a command you want isolated from the bridge but still want to wait on inline:

```bash
systemd-run --wait --pipe --collect bash -c 'cd <workdir>; <command>'
```

`--wait --pipe` streams output back and returns the real exit code — but your
session must stay alive; use Step 1 for anything long.

## Step 4 — Cleanup / re-run

```bash
systemctl reset-failed <job>.service 2>/dev/null || true   # clear a failed unit before re-using the name
systemctl stop <job>.service                               # abort a running job (approval-gated if the job is destructive)
```

## Safety

- This skill detaches *execution*, not *authority*: destructive or approval-gated
  commands still need owner approval before launch — detaching does not bypass gates.
- Never put secrets in the unit name, command line, or log (unit cmdlines are
  visible in `systemctl`/journal); read keys from files (e.g. `~/.hermes/.env`) inside the script.
- Multi-user/rootless nodes: user services may need lingering
  (`loginctl enable-linger`) for the unit to survive logout; on non-systemd
  nodes (Termux) this skill does not apply — use `nohup`/`setsid` + log + EXIT
  marker as the fallback pattern.

## Verification

- Launch returns `Running as unit: <job>.service`
- Log file grows; `EXIT=0` appears at completion
- The job keeps running across a bridge/session restart (that's the point)
