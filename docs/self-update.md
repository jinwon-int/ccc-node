# Self-update (pre-approved node maintenance)

Broker/Gateway/worker service control is `operator_approval_gated` in guard.sh
— and stays that way. But a fleet node is only useful if it can pick up
ccc-node updates and restart its own services. The resolution follows the same
pattern as the `ccc-telegram-bridge` restart carve-out: instead of loosening
the gate, the operator **pre-approves a fixed procedure in code review**, and
agents may invoke that procedure as a whole.

## The procedure

`~/.claude/hooks/ccc-self-update.sh run` (installed by setup.sh):

1. lock; resolve the repo (`CCC_SELF_UPDATE_REPO` > `~/.claude/self-update.repo`
   > script location > `~/ccc-node`)
2. fail-closed preconditions: clean working tree, on the expected branch
   (`CCC_SELF_UPDATE_BRANCH`, default `main`)
3. `git fetch` + `merge --ff-only` (never rewrites local history; diverged →
   abort)
4. if HEAD changed (or `run --force`): `./setup.sh` redeploys the harness; a
   setup failure **rolls back to the old SHA** and aborts
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
  systemd unit per line, `#` comments). guard.sh denies agent writes to this
  file (`self-update-config` gate) — Edit/Write tools *and* shell redirection /
  copy tools — while reads stay allowed. Only the operator decides which units
  the procedure may ever restart.
- Direct `systemctl restart <broker|gateway|worker|…>` remains
  `operator_approval_gated` exactly as before. Nothing was loosened.

## Operator setup (once per node)

```bash
# after git pull && ./setup.sh — write the allowlist YOURSELF (agents cannot):
cat > ~/.claude/self-update.services <<'EOF'
hermes-broker
a2a-worker
ccc-telegram-bridge
EOF
```

From then on, "이 노드 업데이트하고 재시작해줘" over Telegram (or an agent-cron /
A2A fleet rollout) resolves to the guarded, audited
`~/.claude/hooks/ccc-self-update.sh run` — no `CCC_ALLOW_GATED` needed and no
per-restart approval friction.

## Knobs

| Env | Default | Meaning |
|---|---|---|
| `CCC_SELF_UPDATE_REPO` | auto | repo path override (or `~/.claude/self-update.repo`, operator-owned) |
| `CCC_SELF_UPDATE_BRANCH` | `main` | branch the node must be on |
| `CCC_SELF_UPDATE_SERVICES` | `~/.claude/self-update.services` | allowlist path |
| `CCC_SELF_UPDATE_SYSTEMCTL` | `systemctl` | service manager command (tests inject a fake) |

Exit codes: 0 ok/up-to-date · 3 lock held · 4 precondition failed · 5 fetch/ff
failed · 6 setup failed (rolled back) · 7 service restart failure.
