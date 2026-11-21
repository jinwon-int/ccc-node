# Fleet SSH mesh (`scripts/fleet-ssh-mesh.sh`)

Full-mesh SSH between fleet nodes with **one keypair per node**
(`~/.ssh/id_ed25519_fleet`). The script collects the public halves, scans each
node's ed25519 host key and writes three managed blocks on every node, so any
node can `ssh <name>` to any other. Tracking issue: #2687.

## Node table

The node list lives **outside the repo and outside the script**:

```
${CCC_FLEET_SSH_NODES:-$HOME/.config/ccc/fleet-ssh-nodes}    # or --nodes FILE
```

One node per line, whitespace separated, `#` starts a comment:

```
# name    port  user     [host]
node-a    22    root
node-b    2222  root
node-c    8022  u0_a123  node-c.example.internal
```

- `name` is also the ssh alias written into every node's `~/.ssh/config`.
- Without `host`, the address is resolved at run time from
  `tailscale status --json`: the device whose `HostName` or MagicDNS short name
  equals `name`, using its first IPv4 tailnet address. No match, or more than one
  matching device (for example a stale entry left by a reinstall), stops the
  run before any remote action — remove the stale device or set `host`.
- A missing file, a malformed line (wrong field count, bad port, characters
  outside `[A-Za-z0-9._-]`) or a duplicate name stops the run with exit 2.

## Phases

```
scripts/fleet-ssh-mesh.sh [--nodes FILE] [--dry-run] [--allow-partial] PHASE
```

| Phase | What it does |
|---|---|
| `collect` | On each node: create `~/.ssh/id_ed25519_fleet` if missing, print the `.pub`. Only the public key is read; private keys never leave their node. |
| `scan` | `ssh-keyscan -t ed25519` each node's host/port. |
| `build` | Render `block_authorized_keys`, `block_known_hosts`, `block_config` in the work dir (`$CCC_FLEET_SSH_WORK`, default `~/.claude/state/fleet-mesh`). Refuses when fewer keys or host keys than nodes were collected (a push would drop the missing node everywhere) unless `--allow-partial`. |
| `push` | Replace the managed block in `~/.ssh/authorized_keys` (append, 600), `~/.ssh/known_hosts` (append, 644) and `~/.ssh/config` (prepend, 600) on every node. `--dry-run` fetches each file read-only and prints the per-node/per-file change as a diff; nothing is written. |
| `verify` | N×N: from every node, `ssh <alias> echo ok` to every other node. Then, for each node that answered, a read-only check of its `~/.ssh/config` for fleet aliases defined outside the managed block (see below). |
| `all` (default) | `collect`, `scan`, `build`, `push`, stopping at the first failing phase. `verify` is run separately. |

`collect`, `push` and `verify` connect by alias (`ssh <name>`), as defined by
the managed `~/.ssh/config` block on the machine running the script.

## Managed blocks and safety

- Blocks are delimited by `# >>> SEOYOON-FLEET-MESH >>>` /
  `# <<< SEOYOON-FLEET-MESH <<<` (label overridable with
  `CCC_FLEET_SSH_MARKER`); blocks deployed by the earlier unversioned copy of
  the script are recognised and replaced in place of being duplicated.
- Lines outside the block are kept as they are. A file whose markers are
  unbalanced (a begin without an end) is refused, not rewritten.
- Before each remote rewrite the file is copied to
  `<file>.bak-YYYYmmddHHMMSS` (a `.N` suffix is added instead of overwriting).
- Re-running `build` + `push` produces byte-identical files.
- Exit codes: `0` ok, `1` a node or step failed, `2` usage/config/resolution
  error.

## Alias redefined outside the managed block

The managed block is meant to be the only definition of each fleet alias.
`ssh` keeps the first value it reads for each option, so a second `Host` entry
for the same name elsewhere in `~/.ssh/config` can change where the alias
connects. `verify` reads each reachable node's config (one extra
`ssh <node> cat ~/.ssh/config`, nothing is written) and prints one line per
outside `Host` pattern that names an alias the block defines:

```
VERIFY-WARN node-c ~/.ssh/config:1: Host 'node-a' is also defined outside the managed block, before it: ssh keeps the first value of each option, so this outside entry wins over the managed entry at line 5
VERIFY-WARN node-a ~/.ssh/config:28: Host 'node-b' is also defined outside the managed block, after it: shadowed by the managed entry at line 9 for the options that entry sets; IdentityFile lines and options only this outside entry sets still apply
```

- **before** the managed entry: the outside entry wins for every option it
  sets (`HostName`, `Port`, `User`, ...).
- **after** it: the managed entry wins for `HostName`, `Port`, `User` and
  `StrictHostKeyChecking`. An option only the outside entry sets (for example
  `ProxyJump`) still takes effect, and so does every `IdentityFile` line:
  `ssh` offers the keys of both entries, in file order.
- These are warnings. The exit status of `verify` is still the N×N result, and
  no file is changed: remove or rename the outside entry by hand.
- Read as `ssh_config(5)` reads it: keyword in any case, leading blanks,
  `Host=name`, several patterns on one line, quoted patterns, trailing
  `# comment`. Names compare case-sensitively, as in `ssh`.
- Not reported: wildcard or negated patterns (`Host *`, `Host node-*`,
  `Host !x`), `Match` blocks, and files pulled in with `Include`. Any of these
  placed above the managed block can still override it.
- A config whose markers are unbalanced, or that cannot be read, gets a single
  `VERIFY-WARN ... alias check skipped` line. A node that did not answer the
  N×N check is not contacted again.

Tests: `scripts/fleet-ssh-mesh.test.sh` (stub `ssh`/`ssh-keyscan`/`tailscale`
via `SSH_BIN`, `SSH_KEYSCAN_BIN`, `TAILSCALE_BIN`; fake nodes are temp HOMEs).
