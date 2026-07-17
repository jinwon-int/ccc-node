# Risk profiles — ccc-node enforcement model

The harness maps every tool action to one of four risk profiles. The two *gated* profiles
are enforced by `guard.py` (PreToolUse, fail-closed; invoked through the thin `guard.sh`
shim); the other two are non-blocking and captured by observability (`audit.sh` /
`notify.sh`). This implements **separation of approval from execution**: gated actions do
not run without an explicit operator signal.

> Enforcement moved from a hand-rolled bash tokenizer to `guard.py` (issue #452): shlex
> gives real shell tokenization (quotes, `--`, value-taking flags), which the host/target
> parsing below relies on. The contract is unchanged — stdin PreToolUse JSON → exit 2 to
> deny — and is pinned by `guard.test.sh`, which drives the guard as an executable.

| Profile | Behavior | Enforced by | Examples |
|---|---|---|---|
| `autonomous` | Proceeds silently | — (not matched by guard) | read/search, normal `git`/`gh`/`npm`, file edits in repo |
| `operator_notify` | Proceeds; recorded | `audit.sh` (PostToolUse) | any mutating tool call (commit, push to a branch, merge) — auditable after the fact |
| `operator_approval_gated` | **DENIED** until `CCC_ALLOW_GATED=1` | `guard.sh` | DB destructive/migrate/replay, repo visibility, secret read/exfil, secret-file Read/Edit/Write, catastrophic `rm` (service control is no longer gated — see note) |
| `operator_review_gated` | **DENIED**; also needs review evidence | `guard.sh` | force-push *to a protected/ambiguous/multi target*, history rewrite, release/publish/tag-push (changes published/shared state) |

## Bypass (operator approval)

A gated action runs only after the operator approves *that specific action* and sets the
explicit signal:

```bash
CCC_ALLOW_GATED=1 <command>
```

This is the audited bypass. The denial (label + profile + tool, never the raw command) is
recorded to `~/.claude/state/approval-needed.log`; the bypass is logged to stderr.

## Notes

- `operator_review_gated` differs from `operator_approval_gated` only in that the change
  alters shared/published state (remote history, releases), so it warrants review evidence
  in addition to approval. Both are blocked by default.
- **Force-push relaxation** (operator-approved): a *single explicit* force-push to a
  **non-protected feature branch** (e.g. `git push -f origin feat/x`) proceeds
  autonomously — it only rewrites that branch's own history, not shared/published state.
  It stays **DENIED** (review-gated) when the target is a protected branch
  (`main`/`master`/`develop`/`release*`/`hotfix/*`/`prod`/`production`/`stable`), is
  ambiguous/bare (no explicit dst, `HEAD`, current branch), uses multiple refspecs, or is
  part of a compound/chained command. Fail-closed: when the destination can't be parsed
  unambiguously, it is denied.
- **Self-update relaxation** (operator-approved procedure, not a loosened gate): the
  fixed maintenance script `~/.claude/hooks/ccc-self-update.sh` (pull → setup.sh →
  restart of operator-allowlisted units, fail-closed preconditions, audit + rollback)
  runs autonomously — approval happened at PR review time. Its blast-radius boundary,
  `~/.claude/self-update.services` / `self-update.repo`, is write-gated for agents
  (`self-update-config`, Edit/Write tools and shell redirect/copy verbs); reads stay
  allowed. See `docs/self-update.md`.
- **Public-key carve-out** (operator-approved): `*.pub` and `*.pub.pem` files are
  public keys — safe to read — so they are exempt from the secret-file / secret-read
  gates even though they carry a `.pem` extension. Private keys (`*.pem` without
  `.pub`, `*.key`, `id_rsa`) and other secrets stay gated; a public key referenced
  alongside a real secret in the same command still trips the deny.
- **Fleet-service relaxation** (operator-approved): pure lifecycle verbs —
  `start`/`restart`/`reload`/`stop`/`kill` (and `try-`/`-or-` variants) — of fleet
  services (unit/process names carrying `a2a`/`hermes`/`openclaw`/`broker`/`gateway`/
  `worker`, or `ccc-telegram-bridge`) are **not gated**, locally or toward a peer node
  (`ssh <node> systemctl restart <unit>`, `systemctl -H <node> …`). A fleet node manages
  its own and its peers' services so the fleet can update from GitHub and recover
  unattended. Still gated: non-fleet units, config-changing verbs (`enable`/`disable`/
  `mask`/`unmask`/`isolate`/`daemon-*`) even on fleet units, `pm2 delete`, docker/podman/
  kubectl lifecycle, and the down-class of host lifecycle (see *Host-lifecycle* below).
  Compound commands are judged per statement; one non-fleet target denies the whole command.
- **Host-lifecycle / reboot relaxation**: the **reboot-class** — `reboot` and `shutdown -r`
  (the node comes back up) — is autonomous on the **local node** and on **managed nodes**
  (`ssh <managed> reboot`); reboot is disruptive but recoverable. It stays gated for an
  unlisted remote host (`ssh <unlisted> reboot` — list it first) and for interpreter-
  mediated forms (only a *direct* reboot command is relaxed; `python3 -c 'os.system("reboot")'`
  stays gated). The **down-class** — `poweroff`, `halt`, `shutdown` without `-r` — stays
  fresh-approval **everywhere**, including on managed nodes: a powered-off fleet node stays
  offline until manual power-on, so it always warrants a confirm.
- **Managed-nodes relaxation** (operator-owned allowlist; opt-in, fail-closed).
  `~/.claude/managed-nodes.allow` (override `CCC_MANAGED_NODES_ALLOW`) lists the remote
  hosts THIS node operates — one host/glob per line, `#` comments. For a Bash statement
  whose *only* remote reach (via `ssh`/`scp`/`rsync`/`sftp`/`systemctl -H`) is to
  allowlisted hosts, the blast-radius gates are relaxed for that statement: **secret/key
  deployment** (`scp deploy.env node:` — otherwise `secret-exfil`), **remote cleanup**
  (`ssh node "rm -rf /var/log/old"` — otherwise `rm-catastrophic`), **remote service
  control including config verbs** (`ssh node "systemctl daemon-reload"`), and **reboot**
  of the host (`ssh node reboot`; the down-class — `poweroff`/`halt` — stays gated, see
  *Host-lifecycle* above). This is what makes owned nodes manageable without per-command
  `CCC_ALLOW_GATED`. It stays **fail-closed** everywhere else:
    - no allowlist file → behavior is identical to the fleet-only baseline;
    - a host NOT listed → fully gated (genuine exfil to an unknown endpoint still denied);
    - `curl`/`wget`/`nc`/`ncat`/`ftp` are excluded from the relaxation — the `secret-exfil`
      gate keeps full authority over them (deploy with `scp`/`rsync` instead);
    - review-gated classes (force-push to protected, history rewrite, release/publish, DB
      destructive/migrate) are **never** relaxed, even executed via `ssh` on an owned node;
    - a LOCAL destructive op chained alongside a managed remote op is judged on its own
      statement and denied.
  The allowlist is the trust boundary that says "these hosts are mine," so — like
  `self-update.services` — it is **write-gated for agents** (`managed-nodes-config`:
  Edit/Write tools and shell redirect/interpreter writes); reads stay allowed. See
  `docs/service-control.md` and `docs/examples/managed-nodes.allow.example`.
- **Managed-services relaxation** (operator-owned allowlist; opt-in, fail-closed).
  `~/.claude/managed-services.allow` (override `CCC_MANAGED_SERVICES_ALLOW`) lists the
  **local** units/containers/processes this node self-manages. Fleet services are already
  autonomous; this lets the node ALSO control its own **non-fleet** local apps by name —
  `systemctl restart myapp`, `pm2 restart myapp`, `docker restart my-container` — when
  **every** target of the `systemctl`/`service`/`pm2`/`docker`/`podman` lifecycle command
  is listed. Fail-closed otherwise: an unlisted target (or one unlisted target mixed with a
  listed one), targetless/global forms (`daemon-reload`), other Compose lifecycle, a
  docker remote-daemon flag, and command-substitution targets stay gated; `kubectl` is
  never relaxed. Direct local detached reconciliation — `docker compose up -d
  [services...]` or the `docker-compose`/`--detach` equivalents — is autonomous without
  this allowlist. It may be combined only with a literal project `cd`, optional
  rollback `docker tag`, and bounded read-only verification (`docker inspect`,
  `sleep`, loopback-only `curl`). Direct SSH uses the same grammar and requires
  explicit fleet service names. Wrappers, substitutions, arbitrary compound
  commands, multiple reconciliations, non-detached `up`, and remote-daemon
  selection do not enter that carve-out. This keeps `sshd`/`ufw`/`nginx`
  and other system services protected while unblocking the node's own apps. Write-gated for agents
  (`managed-services-config`). Cross-broker mutation still requires fresh
  in-task approval under the standing orders; this carve-out only makes that
  approved runbook executable. See `docs/examples/managed-services.allow.example`.
- Fail-closed: if a pattern is uncertain, prefer gating. Patterns are covered by
  `guard.test.sh` (allow + deny cases, including the force-push relaxation) to avoid
  blocking normal autonomous work.
- This model mirrors the fleet's formal risk profiles and the `CLAUDE.md`
  "Fresh Approval Required" set.

## Memory injection scanner

`scan-injection.sh` protects SessionStart/PostCompact memory injection by redacting
high-risk content before `load-memory.sh` emits `additionalContext`:

- credential-like strings → `[REDACTED:credential]` / `[REDACTED:jwt]`;
- obvious prompt-injection directives → `[REDACTED:prompt-injection]`;
- invisible/bidi Unicode controls → `[REDACTED:unicode]`.

The scanner is **fail-open for availability**: if it is missing or exits non-zero,
`load-memory.sh` injects the original text so a node can still start. Successful findings
are recorded as metadata-only `MemoryInjectionScan` audit events; raw/redacted body text is
never written to the audit log. This is intentionally separate from `guard.sh`, which remains
fail-closed for operator actions.

## Permissions (`settings.json`) vs hook enforcement — decision (#13 item #3)

`settings.json` keeps a broad `Bash(*)` allow **on purpose**: enforcement is the hook
layer, not a permission allowlist. Audit-log analysis (2026-06-21, ~1k tool calls) shows
this node's Bash usage is overwhelmingly **compound / multi-line** — `cd X && …`,
heredocs, `for`/`if` loops, pipes. Claude Code permission entries (`Bash(cmd:*)`)
prefix-match the *whole* command string, so they cannot describe compound commands: a
per-command allowlist would miss most of them and degrade into constant `ask` prompts,
breaking autonomous A2A / cron / headless runs (**over-block**).

Therefore:

- **`allow`** stays broad (`Bash(*)`, `Read/Write/Edit/MultiEdit`), so autonomous work is
  never prompt-blocked.
- **`guard.py`** (PreToolUse, shlex tokenization of the *full* command, behind the
  `guard.sh` shim) is the real Fresh-Approval enforcement — it sees compound commands that
  `settings` prefix patterns cannot, and is fail-closed.
- `settings` `deny`/`ask` carry only patterns that are *expressible* as a simple prefix
  (secret-file `Read`, `npm publish`, `gh release create`, `sudo`) as a declarative second
  line — never the primary guard. Patterns whose safe form is contextual (e.g. force-push,
  allowed to non-protected feature branches but denied elsewhere) are left to `guard.sh`
  to avoid contradicting its policy.

**Decision:** the roadmap's "replace `Bash(*)` allow-all with an allowlist" (#13 item #3)
is **superseded for this node** by the allow-all + fail-closed-hook model above. The
acceptance goal — *every Fresh-Approval category is code-blocked or prompts* — is met by
`guard.sh` and verified by `guard.test.sh`; an allowlist would add over-block risk without
adding enforcement.
