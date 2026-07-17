# Service control

There is **no custom command-guard hook**. ccc-node previously ran a
`claude/hooks/guard.py` PreToolUse policy hook (behind a `guard.sh` shim) to
enforce Fresh-Approval semantics; it has been removed. The Claude path now runs
at Codex parity on Claude Code's native mechanisms, and the enforceable boundary
is the **unprivileged OS account the node runs as** (e.g. `ccc`) plus its
group/sudo grants — exactly like cccnode/Codex — with a root-owned wrapper and
root-owned exact-unit allowlist for the one privileged carve-out (service
restarts).

Two native Claude Code mechanisms shape the path:

- `permissionMode: bypassPermissions` in `claude/settings.base.json` — no
  approval prompts for normal autonomous work. `setup.sh` drops this to a
  prompting mode when the node runs as root, because Claude Code refuses
  `bypassPermissions` as root.
- A small native `permissions.deny` backstop in `claude/settings.base.json`,
  enforced by Claude Code in **every** permission mode (including
  `bypassPermissions`, precedence deny > ask > allow): secret-file reads
  (`.env` / `.credentials.json` / `*.pem` / `id_rsa`), release (`npm publish`,
  `gh release create`), and best-effort catastrophic shapes (`rm -rf /`,
  force-push to `main`).

Those native Bash deny rules are **coarse prefix-globs** — they do not catch
quoting, env-var prefixes, or `&&`-chained variants — so they are a best-effort
catastrophic backstop, **not** the semantic "Fresh Approval Required" wall the
old `guard.py` parser enforced. Fleet-service lifecycle, managed-node
operations, broker Compose runbooks, and cross-broker mutations are no longer
gated by a custom command parser; they are governed by the OS account (its
sudo/group grants, SSH keys, and file ownership).

## Policy

- **Everything the OS account permits runs autonomously.** Under
  `bypassPermissions` there are no approval prompts and no custom command parser
  in the path; the only automated blocks are the native `permissions.deny` rules
  above. Scope what the agent can do by scoping its OS account — sudo grants,
  group memberships (`docker`/`sudo`), and SSH keys — not by editing a guard
  allowlist.
- **Fleet-service lifecycle** (`start`/`restart`/`reload`/`stop`/`kill` of units
  whose name carries `a2a`/`hermes`/`openclaw`/`broker`/`gateway`/`worker` or is
  `ccc-telegram-bridge`, locally or toward a peer via `ssh <node> systemctl …` /
  `systemctl -H <node> …`) runs because the OS account is allowed to run it, so a
  node can update and recover its own and its peers' services unattended. This is
  no longer a guard "relaxation" — nothing parses the command first.
- **Managed-node operations, broker Compose runbooks, and cross-broker
  mutations** are likewise governed only by the OS account. They succeed or fail
  on what that account can actually reach (its SSH keys, `docker`-group
  membership, remote sudo). The old per-statement evaluation — "one non-fleet
  target denies the whole command," the `managed-nodes.allow` /
  `managed-services.allow` opt-in allowlists, and the reboot-vs-poweroff split —
  was `guard.py` behavior and is gone. Bound these by bounding the OS account.
- **The native deny backstop is a seatbelt, not a wall.** `rm -rf /`, force-push
  to `main`, `npm publish`, and `gh release create` are denied only in their
  literal prefix form; quoted, env-var-prefixed, or `&&`-chained variants slip
  past. Secret-file reads (`.env`, `.credentials.json`, `*.pem`, `id_rsa`) are
  denied to the `Read` tool in every mode, but this too is a coarse path-glob,
  not a secret-exfil parser. Do not treat either as Fresh-Approval enforcement.
- Pre-reviewed self-update remains available through `ccc-self-update.sh`; its
  operator config must be root-owned and unavailable for agent writes.
- Where a real privilege boundary is required, keep the agent's OS account
  unprivileged and mediate the one pre-approved privileged action — service
  restarts — through the installed `ccc-service-control` wrapper and exact
  `.service` names in `/etc/ccc-node/service-control.allow` (see below).

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
bash scripts/ccc-service-control.test.sh
bash scripts/validate-harness.sh
```

Host rollout, account/sudoers changes, and service restarts require separate
approval and node-by-node verification.
