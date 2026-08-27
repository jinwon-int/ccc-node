---
name: bridge-safe-detached-run
description: Run a long-running command as a detached systemd transient unit so it survives Telegram-bridge/session restarts — with persistent log file, EXIT marker, HOME/PATH env injection, and a durable job registry that lets any later session read the completion evidence. Use when a task may outlive the current session (installs, mining/indexing, builds, migrations, long tests), when a previous background task was killed by a bridge restart (ccc-node #822), when a watcher died and a finished job looks failed (ccc-node #1258), or when systemd-run output mysteriously fails due to missing HOME.
---

# bridge-safe-detached-run

Session/bridge restarts kill ordinary background children (`&`, harness bg
tasks — ccc-node #822). For any command that may outlive the session, run it as
a **systemd transient unit** owned by PID 1, with a persistent log and an EXIT
marker, then **register it** so the evidence outlives your watcher too. Step 1
formalizes the #822 workaround; Step 2 closes #1258, where the work survived but
the process watching it did not.

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

## Step 2 — Register the job so the *next* session can find it

**Detaching the work is not enough — the watcher must not be the only record**
(ccc-node #1258). A `Monitor` until-loop or a bounded `for` poll runs as a child
of the session process. When the session restarts, the watcher dies while the
job keeps going, and all you get is `<task-notification status=stopped>`, which
cannot distinguish "the work failed" from "my watcher was lost". On 2026-08-24
that nearly caused a completed 12/12 job to be re-run.

Register the log path immediately after launching, in the same turn:

```bash
python3 ~/.claude/hooks/lib/detached_jobs.py register \
  --unit <job> --log "$LOG" --summary "<one line, body-free>"
```

Now the completion evidence is durable and **stateless to read**: every
SessionStart sweeps the registry and reports each job as `done` (with its real
`EXIT=` code), `running`, or `lost` — no surviving process required. Acknowledge
a job you have acted on so it stops being reported:

```bash
python3 ~/.claude/hooks/lib/detached_jobs.py ack --unit <job>
python3 ~/.claude/hooks/lib/detached_jobs.py list     # all outstanding jobs
```

## Step 2b — Watching inside the current turn (optional)

Polling is still fine when you genuinely expect to stay alive, but it is now an
optimization, not the record:

```bash
# quick check
tail -5 "$LOG"; grep -c '^EXIT=' "$LOG"
# blocking watcher (bounded)
for i in $(seq 1 120); do grep -q '^EXIT=' "$LOG" && break; sleep 10; done
grep '^EXIT=' "$LOG"   # EXIT=0 → success; nonzero → inspect log
```

While the unit is live: `systemctl status <job>.service` / `journalctl -u <job> -n 20`.
Remember `--collect` removes the unit on exit, so once the job succeeds
`systemctl status` says "could not be found" — that is normal and is **not**
evidence of failure. The log's `EXIT=` marker is the only durable proof.

## Receiving `<task-notification status=stopped>` — classify first

The watcher stopping is not the job failing (ccc-node #1267): a session or
bridge restart (#822), a dying poll loop, and a lost child all surface as the
same line. The only wrong move is acting on the notification alone — #1258's
near-miss was a finished 12/12 job about to be re-run because of it. Read the
registry instead; it is stateless, so no surviving watcher is required:

```bash
python3 ~/.claude/hooks/lib/detached_jobs.py list --json  # done/running/lost with real EXIT codes
python3 ~/.claude/hooks/lib/detached_jobs.py sweep        # human-readable, byte-capped
```

| verdict | meaning | action |
|---|---|---|
| `done`, `EXIT=0` | completed successfully | report success; `ack --unit <job>` |
| `done`, nonzero/absent EXIT | the work itself failed | inspect the log tail, then decide |
| `running` | work alive — only the watcher died | nothing to fix; optionally start a fresh watcher |
| `lost` | stale log, no EXIT marker | investigate unit/log before any re-run |

If the job was never registered, fall back to raw evidence: `journalctl -u
<job>.service -n 20` while the unit lives (`--collect` removes it afterwards)
plus the log's `EXIT=` marker — then register it so later notifications
classify cleanly. Re-running stays approval-gated whenever the command is
destructive or non-idempotent; a stopped notification never lowers that gate
by itself.

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
- `detached_jobs.py list` shows the job, and a later SessionStart reports it as
  `done`/`running`/`lost` even if this session is gone
- A `status=stopped` notification is answered from `list --json` without
  restarting or re-running anything
