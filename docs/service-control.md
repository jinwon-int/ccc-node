# Fail-closed service control

`claude/hooks/guard.sh` is a defense-in-depth policy hook, **not a sandbox**.
The enforceable boundary is an unprivileged agent account plus a root-owned
wrapper and root-owned exact-unit allowlist.

## Policy

- Direct `systemctl`, `service`, or `pm2` lifecycle commands require fresh
  operator approval.
- Host lifecycle commands (`shutdown`, `reboot`, `poweroff`, `halt`) require
  fresh operator approval.
- The only direct service carve-out is an exact local restart of
  `ccc-telegram-bridge[.service]`.
- Pre-reviewed self-update remains available through `ccc-self-update.sh`; its
  operator config must be root-owned and unavailable for agent writes.
- Other pre-approved restarts use only the installed `ccc-service-control`
  wrapper and exact `.service` names in `/etc/ccc-node/service-control.allow`.

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
