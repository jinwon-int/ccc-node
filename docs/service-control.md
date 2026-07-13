# Fail-closed service control

`claude/hooks/guard.sh` is a defense-in-depth policy hook, **not a sandbox**.
The enforceable boundary is an unprivileged agent account plus a root-owned
wrapper and root-owned exact-unit allowlist.

## Policy

- **Fleet-service lifecycle is autonomous** (operator-approved relaxation; see
  `claude/hooks/RISK-PROFILES.md`): `start`/`restart`/`reload`/`stop`/`kill`
  of units whose name carries `a2a`/`hermes`/`openclaw`/`broker`/`gateway`/
  `worker` or is `ccc-telegram-bridge` — locally or toward a peer node
  (`ssh <node> systemctl restart <unit>`, `systemctl -H <node> …`). A fleet
  node manages its own and its peers' services so it can update from GitHub
  and recover unattended.
- Everything else is fail-closed: `systemctl`/`service`/`pm2` lifecycle of
  non-fleet units, config-changing verbs (`enable`/`disable`/`mask`/`unmask`/
  `isolate`/`daemon-*`), `pm2 delete`, and docker/podman/kubectl lifecycle
  require fresh operator approval. Compound commands are judged per lifecycle
  segment; one non-fleet target denies the whole command.
- Host lifecycle commands (`shutdown`, `reboot`, `poweroff`, `halt`) require
  fresh operator approval — including against peer nodes over ssh.
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
