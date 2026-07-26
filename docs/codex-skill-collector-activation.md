# Codex skill-candidate collector — default-ON operations

The Codex skill-candidate collector (#667, #749) is enabled by default on
**Codex** nodes. Claude nodes never compose the worker. A node-local
`CCC_CODEX_SKILL_COLLECTOR=false` remains the immediate opt-out.

The collector only creates pending drafts. It does **not** change the installer
policy: `CCC_SKILL_AUTOSAVE_MODE` remains `approve` by default, so no skill is
installed without the existing review gate.

## Runtime boundaries

- The collector reads already-captured distill snapshots without claiming or
  mutating distill jobs.
- One provider attempt is allowed per sweep by default, preventing a first
  start from bursting through historical snapshots. Operators may set
  `CCC_CODEX_SKILL_COLLECTOR_MAX_JOBS_PER_SWEEP=1..10`.
- Provider attempts share the node autonomous usage meter. A configured
  `CCC_USAGE_BUDGET_TOKENS_CODEX` blocks the call prospectively when the full
  bounded reservation cannot fit.
- Backend failures use durable exponential backoff (five minutes up to one day)
  and retain only a body-free error code.
- Successful and zero-candidate jobs receive idempotent markers and are not
  charged again.

## Deployment verification

Changing the repository default does not restart or deploy a live node. After
an independently approved node update/restart:

1. Confirm the bridge is healthy:

   ```bash
   /opt/ccc-node/bridge/start.sh --path /root --status
   ```

2. Confirm the node uses the Codex provider and does not carry an intentional
   opt-out:

   ```bash
   grep -E '^(CCC_AGENT_PROVIDER|CCC_CODEX_SKILL_COLLECTOR)=' \
     /root/.telegram_bot/.env
   ```

   Absence of `CCC_CODEX_SKILL_COLLECTOR` means the default (`true`). Never
   print the rest of the env file.

3. Review staged drafts and keep installation in approve mode:

   ```bash
   CCC_SKILL_PROVIDER=codex \
     ~/.claude/hooks/skill-review/autoinstall.sh status
   ls ~/.claude/state/pending-skills/
   ```

4. Check body-free usage/backoff diagnostics for unexpected repeated failures.
   Candidate bodies and transcript text must not be logged.

## Opt out / rollback

Set the explicit node-local override:

```dotenv
CCC_CODEX_SKILL_COLLECTOR=false
```

Apply it only through the approved node configuration/restart procedure. The
collector then stops issuing provider calls; memory distill and already staged
drafts are unchanged.

Pending drafts remain available for normal human review. If the operator had
separately enabled auto-install, marker-owned installs can be rolled back:

```bash
CCC_SKILL_PROVIDER=codex \
  ~/.claude/hooks/skill-review/autoinstall.sh rollback --all
```

Rollback refuses hand-authored skills without the autosave marker.

## Rollout evidence

The original canary is tracked in #673. It exposed and fixed strict structured
output (#675), provenance comparison (#676), and zero-candidate replay (#677)
before the source default changed. Fleet deployment, service restart, provider
send, and release remain separate operational actions.
