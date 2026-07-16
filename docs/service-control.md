# Service control

`claude/hooks/guard.py` (behind the `guard.sh` shim) is a defense-in-depth
policy hook, **not a sandbox**. The enforceable boundary is an unprivileged
agent account plus a root-owned wrapper and root-owned exact-unit allowlist.

## Policy

- **Fleet-service lifecycle is autonomous** (operator-approved relaxation; see
  `claude/hooks/RISK-PROFILES.md`): `start`/`restart`/`reload`/`stop`/`kill` of
  units whose name carries `a2a`/`hermes`/`openclaw`/`broker`/`gateway`/`worker`
  or is `ccc-telegram-bridge` — locally or toward a peer node
  (`ssh <node> systemctl restart <unit>`, `systemctl -H <node> …`).
- **Managed-node operations are autonomous for allowlisted hosts** (opt-in;
  `~/.claude/managed-nodes.allow`): a Bash statement whose only remote reach
  (via `ssh`/`scp`/`rsync`/`sftp`/`systemctl -H`) is to a listed host may deploy
  secrets/keys, clean up remote paths, run remote service **config** verbs
  (`enable`/`daemon-reload`), and reboot that host. See
  `docs/examples/managed-nodes.allow.example`. The allowlist is operator-owned
  and write-gated for agents; with no allowlist the behavior is the fleet-only
  baseline.
- **Reboot is autonomous on the LOCAL node and managed nodes** — `reboot` /
  `shutdown -r` is disruptive but recoverable (the node comes back). It stays
  gated for an unlisted remote host and for interpreter-mediated forms.
- **Local non-fleet apps the node self-manages are autonomous for allowlisted
  units** (opt-in; `~/.claude/managed-services.allow`): `systemctl`/`service`/
  `pm2`/`docker`/`podman` lifecycle of a LOCAL unit/container is allowed when
  every target is listed. System services you did not list (`sshd`/`ufw`/`nginx`/
  …), mixed targets, `daemon-reload`, other `docker compose` lifecycle, and
  `kubectl` stay gated. Direct local detached reconciliation —
  `docker compose up -d [services...]` or `docker-compose up --detach` — is
  autonomous without an allowlist. One reconciliation may be wrapped in the
  fixed operator runbook shape: a literal project `cd`, optional rollback
  `docker tag`, one `up -d`, then `docker inspect`, bounded `sleep`, and
  loopback-only health `curl`. A direct `ssh <peer> "..."` form is also allowed
  when every explicit Compose service is a fleet service. Arbitrary compound
  commands, external or mutating curl, multiple reconciliations, remote-daemon
  flags, wrappers, substitutions, and non-detached `up` stay gated. See
  `docs/examples/managed-services.allow.example`. This guard relaxation makes
  an approved runbook executable; it does not replace the standing fresh
  approval requirement for cross-broker mutations.
- Everything else is fail-closed: `systemctl`/`service`/`pm2` lifecycle of
  non-fleet units on hosts that are not managed, config-changing verbs on the
  local node, `pm2 delete`, and docker/podman/kubectl lifecycle require fresh
  operator approval. Compound commands are judged per statement; one non-fleet,
  non-managed target denies the whole command.
- The down-class of host lifecycle (`poweroff`/`halt`/`shutdown` without `-r`)
  requires fresh operator approval **everywhere** — local, managed, or unlisted —
  because a powered-off node stays offline until manual power-on.
- Pre-reviewed self-update remains available through `ccc-self-update.sh`; its
  operator config must be root-owned and unavailable for agent writes.
- Where a real privilege boundary is required (unprivileged agent account),
  pre-approved restarts use the installed `ccc-service-control` wrapper and
  exact `.service` names in `/etc/ccc-node/service-control.allow`.

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

## Verification

```bash
bash claude/hooks/guard.test.sh
bash scripts/ccc-service-control.test.sh
bash scripts/validate-harness.sh
```

Host rollout, account/sudoers changes, and service restarts require separate
approval and node-by-node verification.
