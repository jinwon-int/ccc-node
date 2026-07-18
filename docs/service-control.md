# Service control

This node runs the native Claude Code posture: there is **no PreToolUse policy
hook** (the semantic guard was removed, TM-1306). The Fresh-Approval /
service-control boundary is now two things working together:

1. **Behavioral policy** the agent self-enforces from `CLAUDE.md` (what is
   autonomous vs. what needs fresh operator approval), and
2. **Real OS-level boundaries** — an unprivileged agent account plus root-owned
   wrappers and root-owned exact-unit allowlists — for anything that must be
   enforceable by the OS rather than by policy.

The catastrophic / injection set is **always** fresh-approval regardless of
lane: catastrophic `rm`, secret exfiltration, force-push/history-rewrite of
protected branches, DB destructive/migrate/replay, release/publish +
repo-visibility, host power-down (`poweroff`/`halt`), and operator-config
writes. These stay behaviorally gated because an unattended prompt injection
reading untrusted input (PRs, web, A2A) could otherwise trigger irreversible
damage; the OS-level boundaries below are the backstop for the destructive
subset.

## Policy (behavioral — self-enforced from CLAUDE.md)

- **Fleet-service lifecycle is autonomous**: `start`/`restart`/`reload`/`stop`/
  `kill` of units whose name carries
  `a2a`/`hermes`/`openclaw`/`broker`/`gateway`/`worker` or is
  `ccc-telegram-bridge` — locally or toward a peer node
  (`ssh <node> systemctl restart <unit>`, `systemctl -H <node> …`).
- **Managed-node operations are autonomous for operator-designated hosts**: a
  statement whose only remote reach (via `ssh`/`scp`/`rsync`/`sftp`/
  `systemctl -H`) is to an operator-managed host may deploy secrets/keys, clean
  up remote paths, run remote service **config** verbs (`enable`/`daemon-reload`),
  and reboot that host.
- **Reboot is autonomous on the LOCAL node and managed nodes** — `reboot` /
  `shutdown -r` is disruptive but recoverable (the node comes back). It stays
  fresh-approval for an unlisted remote host.
- **Local non-fleet apps the node self-manages are autonomous**: `systemctl`/
  `service`/`pm2`/`docker`/`podman` lifecycle of a LOCAL unit/container the
  operator designated as self-managed. System services you did not designate
  (`sshd`/`ufw`/`nginx`/…) stay fresh-approval. Direct local detached
  reconciliation (`docker compose up -d [services...]`) is autonomous, but new
  runbook needs go through the reviewed `ccc-broker-reconcile` wrapper, not
  ad-hoc grammar. This does not replace the standing fresh approval requirement
  for cross-broker mutations.
- Everything else is fresh-approval: `systemctl`/`service`/`pm2` lifecycle of
  non-fleet units on unmanaged hosts, config-changing verbs on the local node,
  `pm2 delete`, and other docker/podman/kubectl lifecycle.
- The down-class of host lifecycle (`poweroff`/`halt`/`shutdown` without `-r`)
  requires fresh operator approval **everywhere** — local, managed, or unlisted —
  because a powered-off node stays offline until manual power-on.
- Pre-reviewed self-update remains available through `ccc-self-update.sh`; its
  operator config must be root-owned and unavailable for agent writes.
- Where a real privilege boundary is required (unprivileged agent account),
  pre-approved restarts use the installed `ccc-service-control` wrapper and
  exact `.service` names in `/etc/ccc-node/service-control.allow`.
- Broker Compose reconciliation uses the installed `ccc-broker-reconcile`
  wrapper (root-owned, so the agent cannot alter the wrapper or its operator
  config) instead of raw `docker compose up -d`. New runbook needs are reviewed
  inside the wrapper.

## Operator installation (not performed by setup.sh)

This is intentionally a separate, reviewed host operation.  Never grant sudo
to the mutable copy inside a Git checkout.

1. Install `scripts/ccc-service-control.sh` as
   `/usr/local/libexec/ccc-service-control`, owned by `root:root`, mode `0755`.
2. Create `/etc/ccc-node/service-control.allow`, owned by `root:root`, mode
   `0600`, with one exact `.service` unit per line.
3. Grant the agent account passwordless sudo only for the installed wrapper.
   Do not use `SETENV`; the wrapper ignores allowlist overrides for real runs.
4. Keep the agent itself unprivileged and unable to write the wrapper,
   allowlist, sudoers entry, or self-update operator config.
5. Do not grant the agent Docker/Podman socket access, membership in the
   `docker` group, or broad Kubernetes credentials. Those are privilege
   boundaries equivalent to direct service/root control.

Example allowlist:

```text
# exact names; globs and aliases are rejected
a2a-hermes-worker.service
ccc-telegram-bridge.service
```

Example sudoers shape (replace `ccc-agent` with the node's dedicated account):

```text
ccc-agent ALL=(root) NOPASSWD: /usr/local/libexec/ccc-service-control restart *
```

The wildcard does not grant arbitrary service control: the immutable wrapper
accepts only `restart <exact-unit.service>` and rechecks the root-owned
allowlist before invoking `/usr/bin/systemctl`.

### Broker Compose reconcile wrapper

Same trust model (never grant to a mutable checkout copy):

1. Install `scripts/ccc-broker-reconcile.sh` as
   `/usr/local/libexec/ccc-broker-reconcile`, owned by `root:root`, mode `0755`.
2. Create `/etc/ccc-node/broker-reconcile.dir`, owned by `root:root`, mode
   `0600`, containing the single absolute broker project directory.
3. Create `/etc/ccc-node/broker-reconcile.allow`, owned by `root:root`, mode
   `0600`, with one exact Compose service name per line.
4. The agent invokes
   `/usr/local/libexec/ccc-broker-reconcile <service> [<service>...]` with exact
   service tokens. The wrapper rechecks itself and both root-owned config files,
   rejects daemon/Compose environment overrides, `cd`s to the fixed project dir,
   exports `A2A_BROKER_REVISION=$(git rev-parse HEAD)`, and runs
   `/usr/bin/docker compose up -d <allowlisted services>`.

Scope note: this wrapper performs no `sudo` and no privilege escalation. Its
purpose is wrapper/config and command-shape **integrity** — keeping the runbook
as a single reviewed root-owned entrypoint — not privilege reduction or
integrity of the broker checkout/Compose payload itself. For unattended
reconciliation the agent account still needs Docker
access, which remains a host-root-equivalent grant (see the note above); the
wrapper does not change that boundary.

## Verification

```bash
bash scripts/ccc-service-control.test.sh
bash scripts/ccc-broker-reconcile.test.sh
bash scripts/validate-harness.sh
```

Host rollout, account/sudoers changes, and service restarts require separate
approval and node-by-node verification.
