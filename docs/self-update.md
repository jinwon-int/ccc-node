# Self-update (pre-approved node maintenance)

A fleet node must be able to pick up ccc-node updates and restart its own
services. Direct service control (`restart`/`start`/`reload`/`stop`/`kill`) of
broker/Gateway/worker units is autonomous behavioral policy on a fleet node.
This script is still the **pre-approved, audited way to do it as one
atomic step** — pull → `setup.sh` → restart the operator-allowlisted set with
fail-closed preconditions and rollback — which an agent may invoke as a whole
rather than composing the steps ad hoc.

## The procedure

`~/.claude/hooks/ccc-self-update.sh run` (installed by setup.sh):

1. lock; resolve the repo (`CCC_SELF_UPDATE_REPO` > `~/.claude/self-update.repo`
   > script location > `~/ccc-node`)
2. fail-closed preconditions: clean working tree, on the expected branch
   (`CCC_SELF_UPDATE_BRANCH`, default `main`); Claude/Hermes/state/repository
   paths must be absolute, normalized, non-root, non-overlapping, and free of
   symlink components; managed artifacts must not be symlinks or hardlinks
3. `git fetch` + `merge --ff-only` (never rewrites local history; diverged →
   abort)
4. if HEAD changed (or `run --force`): snapshot the managed Claude artifacts
   plus Hermes `honcho.json`, then let `./setup.sh` redeploy the harness; a
   setup failure verifies rollback of both the repository SHA and installed
   artifacts before reporting success
5. restarts each service listed in the operator allowlist and verifies it is
   active again
6. appends a JSONL audit record (`~/.claude/state/self-update.log`) and queues
   an owner Telegram notification via the push spool (token never touched;
   delivery needs the bridge `CCC_PUSH_ENABLED=true` opt-in)

`ccc-self-update.sh status` is the read-only inspection mode.

## Why this preserves "separation of approval from execution"

- The **procedure** is approved once, by humans, in PR review — not by chat
  pressure at 2am. An agent can trigger the whole audited pipeline but cannot
  compose its steps differently.
- The **blast radius** is bounded by `~/.claude/self-update.services` (one
  `[user:|system:]unit` per line, `#` comments; omitted scope means `system`).
  This file is operator-owned — the agent
  must not edit it — so only the operator decides which units the procedure may
  ever restart.
- Direct `systemctl restart|start|reload|stop|kill <broker|gateway|worker|…>` is
  autonomous behavioral policy — a node manages its own service lifecycle
  so it can update and recover unattended. This bundled procedure remains the
  audited way to do "update **and** restart the allowlisted set" atomically.

## Operator setup (once per node)

```bash
# after git pull && ./setup.sh — write the allowlist YOURSELF (agents cannot):
cat > ~/.claude/self-update.services <<'EOF'
system:hermes-broker
system:a2a-worker
user:ccc-telegram-bridge.service
EOF
```

From then on, "이 노드 업데이트하고 재시작해줘" over Telegram (or an agent-cron /
A2A fleet rollout) resolves to the guarded, audited
`~/.claude/hooks/ccc-self-update.sh run` — no `CCC_ALLOW_GATED` needed and no
per-restart approval friction.

## Idle gate (don't restart mid-task)

`systemctl restart` SIGTERMs the whole service cgroup. For the telegram bridge
that kills the in-flight `claude` child (exit 143) and destroys the user's work —
so a self-update that lands while the bridge is busy silently interrupts a
running task. To avoid that, the run **defers before touching anything** while
the bridge is serving a request:

- the bridge publishes an in-flight `workload` snapshot to its `health.json`;
- if that snapshot is fresh and shows `active_requests > 0`, the run logs a
  `deferred reason=bridge-busy` audit line and exits `8` — nothing is fetched or
  restarted, and the next scheduled tick retries. **A tick only exists if a
  `self-update` agent-cron task is registered** (see *Scheduling* below);
  otherwise a deferred run is never retried automatically. Note also that
  requesting an update from inside a live bridge conversation marks that very
  conversation busy, so such a request almost always self-defers — let the
  scheduled task apply it, or use `--force`;
- bounded so it can't starve updates: a single task older than
  `CCC_SELF_UPDATE_BUSY_MAX_SECONDS` no longer blocks, and total deferral is
  capped at `CCC_SELF_UPDATE_MAX_DEFER_SECONDS`;
- fail-open (missing / unreadable / stale `health.json` → proceed) and
  `--force` bypasses the gate entirely.

## Scheduling

Self-update does **not** schedule itself. It runs only when invoked — either
manually (`ccc-self-update.sh run`) or by an agent-cron task. `setup.sh`
registers a `self-update` agent-cron task idempotently (opt out with
`CCC_SELF_UPDATE_REGISTER_CRON=false`), but the **agent-cron timer must be
installed separately** (`scripts/install-agent-cron-systemd.sh`); without it
the registered task never fires. (Before #909 the task was never registered,
so a deferred run had no tick to retry on.)

The default schedule is `17 4,10,16,22 * * *` (four times daily, off-zero
minute); override with `CCC_SELF_UPDATE_CRON`. The task uses
`--success-exit-codes 0,8,11`, so a clean update (0), a bridge-busy defer (8),
and a no-services-allowlist degraded run (11) do not raise on-failure alerts —
only real aborts do. To verify registration: `agent-cron.sh list | grep
self-update`.

## Knobs

| Env | Default | Meaning |
|---|---|---|
| `CCC_SELF_UPDATE_REPO` | auto | repo path override (or `~/.claude/self-update.repo`, operator-owned) |
| `CCC_SELF_UPDATE_BRANCH` | `main` | branch the node must be on |
| `CCC_SELF_UPDATE_SERVICES` | `~/.claude/self-update.services` | allowlist path |
| `CCC_SELF_UPDATE_SYSTEMCTL` | `systemctl` | service manager command (tests inject a fake) |
| `CCC_SELF_UPDATE_HEALTH_FILE` | `~/.telegram_bot/health.json` | bridge health file the idle gate reads |
| `CCC_SELF_UPDATE_HEALTH_FRESH_SECONDS` | `90` | max age of `health.json` for its workload to count |
| `CCC_SELF_UPDATE_BUSY_MAX_SECONDS` | `1800` | never defer for a task older than this |
| `CCC_SELF_UPDATE_MAX_DEFER_SECONDS` | `3600` | cap total deferral so continuous load can't starve updates |

Exit codes: 0 ok/up-to-date · 3 lock held · 4 precondition failed · 5 fetch/ff
failed · 6 setup/snapshot failed (repo and managed artifacts were verified
rolled back, or setup never started) · 7 service restart failure · 8 deferred
(bridge busy — retry next tick) · 9 repository or installed-artifact rollback
was degraded · 10 successful-update recovery snapshot cleanup failed ·
11 degraded — code updated but no allowlisted service restarted (services
file missing/empty); running processes may still hold the old code, so this
is reported as non-ok to surface silent drift rather than `result:"ok"`.
On exit 9, the validated private recovery snapshot is retained under
`~/.claude/state/self-update-install-rollback.*/` (`0700` directory containing
`0600` Claude and Hermes archives) for local operator
recovery only; do not share it because it may contain settings and memory
files. Exit 10 likewise reports and retains the path rather than recording a
false success. Normal success and successful rollback remove it automatically.

`setup.sh` independently exits `70` when its own local artifact rollback is
degraded and prints the retained private transaction directory. The outer
self-update layer must still verify its own repository + Claude + Hermes
rollback rather than treating that exit as a complete restore.
