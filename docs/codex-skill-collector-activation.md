# Codex skill-candidate collector — activation runbook (canary)

The Codex skill-candidate collector (#667) is implemented and **off by default**
(`CCC_CODEX_SKILL_COLLECTOR`). Enabling it is a **node-local operational change**
— not a repo change — and follows a canary rollout. See
[`skill-autosave.md`](skill-autosave.md) for what the collector does. Rollout is
tracked in the ops issue for #667.

## Preconditions

- The node runs the **Codex** provider (`agent_provider == "codex"`).
- The distill journal is active (the collector reads its snapshots read-only).
- You have operator approval to activate on this specific node.

## Why this needs a canary

`build_context` and `bot_lifecycle` run on **every** node's startup. The
collector is three-guarded (Codex node **and** flag on **and** a distill
journal), so a default node is unaffected — but flag activation changes runtime
behavior and CI cannot smoke-test a real bridge start. Confirm startup on one
node before widening.

## Activation steps

1. **Baseline** — confirm the bridge is healthy now:

   ```bash
   /opt/ccc-node/bridge/start.sh --path /root --status
   ```

2. **Enable the flag** in the node-local bridge env (project `.env`, e.g.
   `/root/.telegram_bot/.env`) — never in the repo default:

   ```dotenv
   CCC_CODEX_SKILL_COLLECTOR=true
   # Optional: point the sink at the Codex autoinstall PENDING_DIR if non-default
   # CCC_SKILL_REVIEW_PENDING_DIR=/root/.claude/state/pending-skills
   ```

3. **Restart** the bridge and **verify clean startup** (no tracebacks; the
   `skill-candidate-collector` loop task starts):

   ```bash
   /opt/ccc-node/bridge/start.sh --path /root --restart
   /opt/ccc-node/bridge/start.sh --path /root --status
   ```

   If startup fails, roll back immediately (below).

4. **Review the first drafts** — after some Codex sessions checkpoint, staged
   candidates appear under the pending-skills dir. Confirm they are
   redaction-safe and well-formed **before** trusting auto-install:

   ```bash
   ~/.claude/hooks/skill-review/autoinstall.sh status   # CCC_SKILL_PROVIDER=codex
   ls ~/.claude/state/pending-skills/
   ```

   Keep autoinstall in **approve** mode (the default) for the canary; only move
   to `auto` after the drafts look right.

5. **Observe** for an agreed window (e.g. 3–7 days): no startup regressions, no
   secret/path leakage in staged drafts, no runaway spend (metered by #388).

6. **Widen** to more nodes only after the canary is clean, one node at a time.

## Rollback (always available)

Disable the flag and restart — the collector stops; nothing else changes:

```dotenv
CCC_CODEX_SKILL_COLLECTOR=false
```

```bash
/opt/ccc-node/bridge/start.sh --path /root --restart
```

Any drafts already staged remain in the pending queue for normal human review;
none were auto-installed unless autoinstall was explicitly in `auto` mode. To
also undo auto-installed skills, use the marker-driven rollback:

```bash
CCC_SKILL_PROVIDER=codex ~/.claude/hooks/skill-review/autoinstall.sh rollback --all
```

## Safety boundary

- Flag activation is per-node and reversible; **do not** flip the fleet default.
- No provider send / release / secret movement is part of activation.
- Enabling on a node requires fresh operator approval for that node.
