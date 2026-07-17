# Service control

`claude/hooks/guard.py` (behind the `guard.sh` shim) is a defense-in-depth
policy hook, **not a sandbox**. The enforceable boundary is an unprivileged
agent account plus a root-owned wrapper and root-owned exact-unit allowlist.

## Operational-relax profile (fresh-root default, operator-owned)

Without a valid profile, the guard enforces the full Fresh-Approval boundary
below. A genuinely fresh root-run `setup.sh` install seeds the root-owned
`/etc/ccc-node/guard-profile` by default. Use `--strict-guard` to opt a fresh
root install out. An existing profile-less strict root node stays strict during
routine setup/self-update and can be explicitly relaxed later with
`sudo ./setup.sh --operational-relax`. The profile contains the line
`operational-relax`; see `docs/examples/guard-profile.example`. When present
and valid, the guard treats **all** service/container/orchestrator lifecycle
(`systemctl`/`service`/`pm2`/`docker`/`podman`/`kubectl`
start·stop·restart·reload·scale·rollout·…, local or toward any peer) and
**reboot of any host** as autonomous.

Non-root setup never installs the profile, and a non-root explicit opt-in is
rejected before managed files change. Setup validates the destination for
symlink components and atomically creates the profile with mode `0644`. It never
overwrites an existing operator profile, including an intentionally fail-closed
malformed file or symlink.

### Operator-approved acceptance criteria (2026-07-17)

- Fresh root ccc-node install: seed `operational-relax` by default.
- Fresh root strict exception: require the explicit `--strict-guard` opt-out.
- Existing profile-less install: do not widen during routine setup/self-update.
- Existing strict root install: retain `--operational-relax` as an explicit
  later opt-in.
- Existing operator file or symlink: never overwrite it.
- Review may harden UID/path/atomicity handling, but changing these default and
  opt-in/opt-out semantics requires a new explicit operator decision.

The profile is **fail-closed and cannot self-escalate**: it is honored only when
the file is owned by `root` (uid 0), a regular non-symlink, and not group/world
writable — the unprivileged agent cannot write a root-owned `/etc` file, and the
guard additionally denies writes to the path. Absent, malformed, or weaker-owned
→ strict.

On a root-run node, root ownership cannot isolate the profile from the agent;
the guard remains a policy hook rather than an OS privilege boundary. Run the
agent unprivileged when self-enable resistance must be enforceable by the OS.

It **never** relaxes the catastrophic / injection set, regardless of the
profile: catastrophic `rm`, secret exfiltration, force-push/history-rewrite of
protected branches, DB destructive/migrate/replay, release/publish +
repo-visibility, host power-down (`poweroff`/`halt`), and operator-config
writes. A mixed remote body containing both reboot and any down-class command
is down-class and remains gated. Those stay enforced because an unattended
prompt injection reading untrusted input (PRs, web, A2A) could otherwise trigger
irreversible damage.

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
  `docker tag`, one `up -d`, then `docker inspect`, `sleep` bounded to 300
  seconds, and
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
- Broker Compose reconciliation uses the installed `ccc-broker-reconcile`
  wrapper (root-owned, so the agent cannot alter the wrapper or its operator
  config) instead of raw `docker compose up -d`. This moves the fixed command
  shape out of the PreToolUse guard's ALLOW-grammar: new runbook needs are
  reviewed inside the wrapper, not
  added as guard grammar. The legacy inline Compose grammar remains accepted
  during migration and is removed once the wrapper is deployed fleet-wide.

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
   `/usr/local/libexec/ccc-broker-reconcile <service> [<service>...]`; the guard
   accepts only that direct absolute path and exact service tokens. The wrapper
   rechecks itself and both root-owned config files, rejects daemon/Compose
   environment overrides, `cd`s to the fixed project dir, exports
   `A2A_BROKER_REVISION=$(git rev-parse HEAD)`, and runs `/usr/bin/docker compose
   up -d <allowlisted services>`.

Scope note: this wrapper performs no `sudo` and no privilege escalation. Its
purpose is wrapper/config and command-shape **integrity** and removing the
runbook from the guard's inline Compose grammar — not privilege reduction or
integrity of the broker checkout/Compose payload itself. For unattended
reconciliation the agent account still needs Docker
access, which remains a host-root-equivalent grant (see the note above); the
wrapper does not change that boundary.

## Verification

```bash
bash claude/hooks/guard.test.sh
python3 claude/hooks/guard-profile.test.py
bash scripts/ccc-service-control.test.sh
bash scripts/ccc-broker-reconcile.test.sh
bash scripts/validate-harness.sh
```

Host rollout, account/sudoers changes, and service restarts require separate
approval and node-by-node verification.
