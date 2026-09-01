# Skill registry

`skills/registry.json` is the generated, CI-enforced single view over every
repo skill source (#1338). It exists so that "what skills exist, where do they
belong, and what state are they in" has one authoritative answer that is
re-derived from the tree — never maintained by hand.

## What it covers

Every canonical skill lives under the single root `skills/` (#1393); codex and
piri keep provider-specific roots. The `audience` of a `skills/` skill is not
derived from a directory any more — each skill's classification entry in
`codex/compatibility.json` carries an explicit `audience` field (`claude` or
`shared`), and the registry reads it from there.

| Audience | Root | Notes |
|---|---|---|
| shared | `skills/` (via catalog `audience`) | runtime-neutral; compatibility-classified |
| claude | `skills/` (via catalog `audience`) | Claude-coupled; compatibility-classified |
| codex | `codex/skills` | Codex-adapted managed ports; the provisioning contract stays in `scripts/ccc_codex_skills.py` |
| piri | `piri/skills` | Piri-native; outside the compatibility catalog |

Each entry carries: source path, audience, classification (from
`codex/compatibility.json`; `null` where the catalog does not reach), managed
membership (an exact `managed_skills[].source` match, not a name match — the
same skill name can exist under two audiences), lifecycle status, description,
file count, and a content tree hash (SHA-256 over sorted relative paths and
per-file hashes).

## Commands

```bash
python3 scripts/ccc-skill-registry.py render  --repo-root .   # print derived JSON
python3 scripts/ccc-skill-registry.py validate --repo-root .  # artifact freshness + tree checks
python3 scripts/ccc-skill-registry.py update --repo-root .    # rewrite the artifact atomically
```

- **validate** re-derives the registry and compares it byte-for-byte with the
  committed artifact; it also checks per-skill frontmatter (`name` equals the
  directory name, `description` 20–1024 chars, at least a minimal body,
  optional `status`), classification coverage for the classified audiences,
  and that every `managed_skills` source resolves to a scanned skill.
- **update** writes via tempfile + fsync + rename at mode 0644, refuses to
  write over an invalid tree, and is byte-idempotent (a no-op re-run reports
  `written: false`).
- `validate-harness` runs validate plus
  `scripts/ccc-skill-registry.test.sh` on every local/CI run, so a stale or
  uncommitted artifact fails closed.

File enumeration prefers git's view (with a filesystem-walk fallback for
tarball installs), mirroring `ccc_codex_skills.py`: stray untracked files in a
long-lived node checkout must not make local validation answer a different
question than CI.

## Adding or changing a skill

1. Add or edit the skill files.
2. Add or update the `compatibility.json` classification (shared/claude
   skills).
3. Run `python3 scripts/ccc-skill-registry.py update --repo-root .` and commit
   the artifact in the same change.

The previous four-place catalog tax (classifications + `managed_skills` +
count assertions + remembering the rest) collapses: the managed-skill count
assertions in `scripts/ccc_codex_skills_test.py` are derived from the
registry, so the irreducible work is skill files + catalog entry + one update
run. `agents/openai.yaml` remains a manual per-managed-skill asset validated
by the provisioner.

## Lifecycle status

`SKILL.md` frontmatter accepts `status: active` (default) or
`status: deprecated`:

- the registry records the state and validate enforces the vocabulary;
- a deprecated managed skill is no longer planned or installed by the Codex
  provisioner on any node; removing its catalog entry remains a separate
  explicit operator step;
- deprecation is repo vocabulary only — node-local learned skills are out of
  scope here; they live under `~/.claude/skills` and reach a repo only through
  the fleet-skills promotion boundary (`docs/skill-autosave.md`).

## Boundaries

- The registry is a derived view, never a source: never hand-edit
  `skills/registry.json` — re-run update.
- It contains only content already public in this repository (descriptions,
  paths, hashes); no secrets, no generated skill bodies.
- Fleet-wide approval and private learned-skill intake remain in
  `jinwon-int/fleet-skills`; this registry describes this repository only.

## Skill precedence

Skill directories have one owner; every tool respects the highest layer and
never fights it (#1344):

```
repo-managed (setup.sh manifest / .ccc-node-managed.json)
  > fleet-approved (.ccc-fleet-skill.json)
    > autosave-owned (installed-by=autosave ledger)
      > user-owned (no tool touches)
```

`ccc-fleet-skills-sync.py` reports `skip-repo-managed` for targets the repo
layer owns (claude root: manifest membership; codex root: provisioner
marker) instead of failing, and setup absorbs fleet-installed copies of repo
skills with a log line. The full contract, the graduation checklist, and the
fleet-skills retirement procedure live in
[`skill-graduation.md`](skill-graduation.md).

## Usage telemetry & monthly audit

`claude/hooks/skill-usage-log.sh` (PostToolUse `Read|Skill`) appends one
owner-only line per skill load to
`$CCC_CLAUDE_DIR/state/skill-usage/usage.jsonl` — the Skill tool and Read of
any `*/skills/*/SKILL.md` both count, which is how bridge-resolved skills
actually load. Best-effort by contract: every failure exits 0 and never
blocks a read.

Monthly retroactive audit (first cycle: 2026-08-28, #1347):

1. `bash ~/.claude/hooks/skill-usage-log.sh report 30` — per-skill load
   counts for the window.
2. Diff against the registry: zero-load skills become cull/archive/keep
   candidates, decided with the graduation checklist's evidence bar
   ([`skill-graduation.md`](skill-graduation.md)).
3. Archive demotions land in `docs/archive/skills/`; removals re-render the
   registry (files + update command).

Known gap: bridge sessions with audience-isolated settings may bypass host
hooks — a bridge-side log line is the follow-up.
