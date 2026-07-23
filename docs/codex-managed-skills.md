# Codex managed skills

`setup.sh` provisions a small, explicit set of repo-shipped operating skills
under `${CODEX_HOME:-$HOME/.codex}/skills`. This is static harness
provisioning, not the learned-skill pipeline tracked by issue #643.

## Compatibility catalog

`codex/compatibility.json` classifies every file under:

- `claude/commands`
- `claude/skills`
- `claude/agents`
- `claude/hooks`

Each asset is `shared`, `adapted`, `claude-only`, `codex-only`, or
`unsupported`. `scripts/ccc_codex_skills.py validate` fails when a command or
other asset is unclassified, matches multiple rules, or a managed Codex skill
contains a harness-specific reference that Codex cannot use.

The managed set is:

| Skill | Purpose |
|---|---|
| `ccc-doctor` | Provider-aware harness diagnosis and repair preview |
| `ccc-node-status` | Read-only source, bridge, provider, and scheduler status |
| `ccc-security-audit` | Read-only security audit interpretation |
| `ccc-agent-cron` | Scheduled-task inspection and explicit execution boundary |
| `ccc-self-update` | Drift check and approval-gated transactional update |
| `ccc-wiki-record` | Family Wiki PR-first durable recording |
| `gh-pr-flow` | Exact-head protected PR review and normal squash merge |

Claude-native lifecycle hooks, sub-agent definitions, MCP registration, and
transcript skill-autosave assets are not copied or presented as Codex features.

## Provisioning contract

Preview without creating `CODEX_HOME`:

```bash
./setup.sh --dry-run
# or:
python3 scripts/ccc_codex_skills.py plan \
  --repo-root . --codex-home "${CODEX_HOME:-$HOME/.codex}"
```

Apply through `setup.sh`, or run the scoped provisioner directly:

```bash
python3 scripts/ccc_codex_skills.py apply \
  --repo-root . --codex-home "${CODEX_HOME:-$HOME/.codex}"
```

Every installed skill contains `.ccc-node-managed.json` with its source path,
file hashes, and aggregate source hash. Directories are 0700 and files are
0600. Re-running with the same source is byte-idempotent.

The provisioner preflights the entire set before writing:

- an existing skill without ccc-node provenance is an unmanaged collision and
  is never overwritten;
- symlinks, hardlinks, wrong owner, unsafe modes, malformed provenance, and
  manual drift fail closed;
- a legitimate repo source update replaces only an intact managed skill;
- all changed skills stage before commit, and partial commit failure restores
  the previous complete set.

No user-authored skill is removed. Rollback of a setup source update is to
restore the prior ccc-node checkout and rerun this provisioner; intact
provenance lets it converge back transactionally.

## Scope boundary

This static catalog does not collect sessions, generate candidates, switch
`off|approve|auto`, or install learned skills. Those remain #643. Provider-aware
doctor reporting and scoped repair of managed-skill drift remain the next #647
slice. `gh-pr-flow` never moves a GitHub credential: its approval helper runs
`gh` on Seoseo with the root-owned isolated config and owner-only credential
file, and returns only body-free gate results.
