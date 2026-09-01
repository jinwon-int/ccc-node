# Skill graduation (fleet-skills → ccc-node)

The skill distribution network uses two repositories with different trust
levels, and graduation is the one-way path between them:

| Repository | Role | Trust level |
|---|---|---|
| **ccc-node** (`skills/`, plus `codex/skills`, `piri/skills`) | Official distribution — hand-written, reviewed skills; `setup.sh` installs them on every node | product-code level |
| **fleet-skills** (private, `approved/`) | Quarantine — skills nodes learned automatically (autosave → promotion → intake PR) | isolated verification |

Auto-generated content never enters the public repository directly
(privacy, review cost, blast radius). But `approved/` is a **verification
station, not a final home**: once a skill has proven itself in the field it
graduates into ccc-node, and the fleet-skills copy retires.

## Graduation checklist (all required)

- [ ] **Dwell time**: at least 2 weeks in fleet-skills `approved/` after its
      intake PR merged, with recorded real usage (which nodes ran it).
- [ ] **No node-specific facts**: `/home/<user>` paths, IPs, emails,
      internal hostnames — the autosave gates check this at intake; re-check
      by eye before making it public.
- [ ] **Secret & privacy review**: graduation publishes the skill into a
      PUBLIC repository. This is irreversible — do the final scan here.
- [ ] **Frontmatter standard** (Agent Skills spec): `name` equals the
      directory name, `description` 20–1024 chars, `status: active`;
      optional `license`, `compatibility` (<=500 chars), `metadata`, and
      `allowed-tools` fields are spec-conformant when present.
- [ ] **Progressive disclosure conformance**: SKILL.md <=500 lines; longer
      content is split into `references/` with read-when pointers; reference
      files >300 lines carry a table of contents.
- [ ] **License & attribution decided for PUBLIC**: third-party content is
      attributed; the skill's license field states the chosen license.
- [ ] **Runtime classification decided**: the compatibility catalog entry
      states `compatibility` and `audience` — `"audience": "shared"`
      (runtime-neutral) or `"audience": "claude"` (Claude-specific references
      present). The skill must pass the compatibility catalog's classification
      for `skills/`.
- [ ] **Operator sponsorship**: a human names this candidate and owns the
      promotion PR. There is no automatic graduation.
- [ ] **Effect evidence (optional, recommended)**: with-skill vs baseline
      comparison recorded per the `skill-creator` evaluation method.

## Procedure

1. **Promotion PR in ccc-node** (authored per `gh-pr-flow`):
   - copy the skill files from fleet-skills `approved/<audience>/<name>/`
     into the canonical root (`skills/<name>/`);
   - add the `compatibility.json` classification with `compatibility` and
     `audience` for the skill;
   - run `python3 scripts/ccc-skill-registry.py update --repo-root .` and
     commit the refreshed `skills/registry.json`;
   - PR body must record the provenance: the fleet-skills intake PR number
     and `source_tree_sha256` from its `approval.json`.

   Promotion PR body template:

   ```markdown
   Graduates <name> from fleet-skills into <target-dir>.

   - Provenance: jinwon-int/fleet-skills#<intake-PR>
     (source_tree_sha256: <hash>, reviewed_by: <reviewer>)
   - Fleet verification: <nodes that ran it>, <duration in approved/>
   - Privacy/secret review: <done, by whom>
   ```

2. **After the PR merges**: nodes receive the skill on their next
   setup/self-update. The repo layer now owns the skill — see precedence
   below.

3. **Retire the fleet-skills copy**: move
   `approved/<audience>/<name>/` to `graduated/<audience>/<name>/` in
   fleet-skills (keep `approval.json`; git history stays the record) and note
   the ccc-node PR in the moved entry. Removing it from `approved/` stops
   `ccc-fleet-skills-sync.py` from installing it — sync only enumerates
   `approved/`. Do this only after the precedence contract below is
   deployed on the nodes that run sync, so the transition window cannot
   error.

## Precedence contract

Skill directories can be claimed by several layers; each tool must respect
the highest-precedence owner and never fight it:

```
repo-managed  (setup.sh install / codex provisioner; provenance:
               repo-skills.manifest entry or .ccc-node-managed.json)
  > fleet-approved  (ccc-fleet-skills-sync.py; .ccc-fleet-skill.json)
    > autosave-owned  (learning pipeline; installed-by=autosave ledger)
      > user-owned  (operator hand edits — no tool touches these)
```

| Tool | Behavior under the contract |
|---|---|
| `ccc-fleet-skills-sync.py` | target carries repo ownership (manifest entry for the claude root, `.ccc-node-managed.json` for the codex root) → reports `skip-repo-managed`, installs nothing. A fleet marker-less target without repo ownership stays `target_user_owned` (fail-closed). |
| `setup.sh` (`install_repo_skills_into`) | a fleet-installed copy of a repo skill is absorbed: repo bytes replace it, the fleet marker disappears, and the absorption is logged. |
| autosave installer | already refuses to overwrite non-autosave directories (dedup gate). |

The transition window (a graduated PR merged on main, nodes not yet
re-run setup) is safe by construction: fleet-sync keeps updating the
still-marked fleet copy until setup absorbs it, and after absorption sync
skips. Both orders converge to exactly one repo-managed copy.

## History

- Designed in #1344 after the registry landed (#1338/#1340) and the local
  review helpers were unified (#1341/#1343).
- First graduation candidates: fleet-skills intake PRs #17–#20
  (gongmyoung/claude, 2026-08-28).
