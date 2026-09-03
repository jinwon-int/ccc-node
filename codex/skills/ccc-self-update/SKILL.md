---
name: ccc-self-update
description: Check ccc-node source and installed harness drift, preview a transactional update, and apply it with backup and rollback verification. Use when asked to update, upgrade, synchronize, or compare a ccc-node harness with GitHub main; applying always requires explicit operator approval.
---

# CCC Self Update

Bring a ccc-node serving checkout and its installed harness up to GitHub main
through **two independent mutation gates**, each requiring its own explicit
operator approval:

- **Gate A — repo sync** (`git pull --ff-only` on the serving checkout)
- **Gate B — harness apply** (`setup.sh` overwrites managed harness files)

Steps 1–2 and the preview are read-only. Never collapse Gate A and Gate B into
one approval.

## 0. Command-surface preflight (read-only, run first)

The procedure is only as executable as its pinned tools. Verify every surface
exists and accepts its pinned flag **before** asking for any approval; if any
check fails, stop and report the missing/renamed surface — do not seek approval
for a procedure that would partially execute:

```bash
ROOT="${CCC_NODE_ROOT:-/opt/ccc-node}"
for f in scripts/ccc-bridge-locate.sh scripts/validate-harness.sh setup.sh; do
  [ -f "$ROOT/$f" ] || { echo "PREFLIGHT FAIL: missing $ROOT/$f"; exit 1; }
done
bash "$ROOT/scripts/ccc-bridge-locate.sh" --json >/dev/null || { echo "PREFLIGHT FAIL: --json unsupported"; exit 1; }
grep -q -- '--dry-run' "$ROOT/setup.sh" || { echo "PREFLIGHT FAIL: setup.sh --dry-run unsupported"; exit 1; }
```

Verification record (pinned at ccc-node main `64fce88`, 2026-09-03 KST):

| Surface | Proof |
|---|---|
| `scripts/ccc-bridge-locate.sh --json` | usage `--json` documented at lines 16–17; flag parsed at line 36 |
| `setup.sh --dry-run` | usage at line 13; flag parsed at line 80; `[dry-run]` output prefix at line 161 |
| `scripts/validate-harness.sh` | run by CI ("Validate harness": JSON, shell syntax, shellcheck, hook tests, frontmatter — `.github/workflows/ci.yml`) |
| durable operator backup | `backup_claude_dir()` — setup.sh:412, writes `~/.claude/backups/ccc-node-setup-<ts>.tar.gz`, archive validated with `tar -tzf` |
| transactional rollback | `begin_install_transaction()` setup.sh:221–237; `rollback_install_transaction()` setup.sh:239–255 (auto-runs via exit trap) |

Re-verify this table against the checkout after each Gate A sync; if a line
reference drifted, re-confirm the surface by hand before Gate B.

## Procedure

1. **Detect drift** (read-only — fetches, mutates nothing):
   ```bash
   ROOT="${CCC_NODE_ROOT:-/opt/ccc-node}"
   bash "$ROOT/scripts/ccc-bridge-locate.sh" --json
   git -C "$ROOT" fetch origin main
   git -C "$ROOT" status --short --branch
   git -C "$ROOT" log --oneline HEAD..origin/main
   git -C "$ROOT" diff --name-status HEAD..origin/main
   ```
   **Affected harness assets** = rows of `diff --name-status` whose path
   intersects the managed set `settings.json hooks output-styles headless.sh
   agents commands skills CLAUDE.md memories` (canonical list:
   `scripts/lib/harness-paths.sh:12-14`, `CCC_MANAGED_PATHS`). Changes outside
   that set are repo-only (no Gate B effect).

2. **Stop conditions — check before any approval ask.** Stop and remediate if:
   - the checkout is **dirty** → a ff-only pull can fail mid-sync, and local
     edits would be silently overwritten by the Gate B apply. Remediate:
     `git stash -u` (or commit), re-inspect, then restart at step 1.
   - the branch **diverged** (non-ff) → `pull --ff-only` is designed to fail
     rather than rewrite history. Remediate: compare `HEAD` vs `origin/main`,
     reconcile (rebase or reset after owner confirmation), or escalate.
   - preflight (step 0) failed → report the missing surface, escalate.

3. **Drift report → Gate A approval.** Report in this shape so runs stay
   comparable:
   ```
   pending commits:   <N> (one per line from step 1 log)
   affected assets:   <managed paths from step 1 diff, or "none (repo-only)">
   proposed action:   Gate A — git -C "$ROOT" pull --ff-only
   approval request:  explicit yes/no for Gate A only
   ```
   Do not proceed without an explicit yes to Gate A.

4. **Gate A — sync the repo** (only after Gate A approval):
   ```bash
   git -C "$ROOT" rev-parse HEAD        # record OLD_SHA — the rollback target for step 8
   git -C "$ROOT" pull --ff-only
   ```
   Expected: ff merge summary, working tree still clean. On failure: fix per
   step 2 remediation and restart at step 1.

5. **Preview the install** (read-only):
   ```bash
   "$ROOT/setup.sh" --dry-run
   ```
   Every line is prefixed `[dry-run]`; nothing is written. Report the dry-run
   plan in this shape:
   ```
   would-write:   <managed paths setup.sh would overwrite>
   skill deltas:  <Codex managed skills changes, incl. catalog updates>
   unchanged:     <assets with no pending diff>
   approval ask:  explicit yes/no for Gate B only
   ```

6. **Gate B — apply with backup** (only after a separate explicit yes):
   ```bash
   ls -t ~/.claude/backups/ccc-node-setup-*.tar.gz 2>/dev/null | head -1   # prior newest, if any
   "$ROOT/setup.sh"
   ```
   setup.sh itself takes the backups — the operator never passes `--no-backup`:
   a durable restore point at `~/.claude/backups/ccc-node-setup-<ts>.tar.gz`
   (validated with `tar -tzf`), plus a private pre-apply transaction snapshot
   that powers automatic rollback. Verify after apply:
   - exit code 0, and the closing "Resolved path configuration" block printed;
   - a **new** archive now exists: `ls -t ~/.claude/backups/ccc-node-setup-*.tar.gz | head -1`
     is newer than the prior newest recorded above.
   If the new backup is absent, treat the apply as unverified: stop, report,
   do not declare success (rollback path below still exists via the internal
   transaction snapshot only while setup ran).

7. **Validate**:
   ```bash
   bash "$ROOT/scripts/validate-harness.sh"
   git -C "$ROOT" status --short --branch
   ```
   Expected: validate-harness passes all sections; working tree clean on the
   expected branch. Then advise starting a fresh session to confirm
   hook/memory injection before declaring success.

8. **Rollback on failure.** If Gate B fails, setup.sh auto-restores the prior
   installed artifacts from the transaction snapshot. Verify the restore
   instead of assuming it:
   - expected stderr: `ERROR: setup failed; restored previous installed
     artifacts (Claude harness, honcho.json, Codex GitHub policy config)` —
     if instead it reports rollback was **degraded**, preserve
     `.ccc-node-setup-rollback.*` next to the harness dir and escalate
     immediately;
   - re-run step 7's validate-harness — it must pass against the restored
     harness;
   - the repo is still at the Gate A head while the harness is at the previous
     state: re-align with `git -C "$ROOT" reset --hard <previous-sha>`
     (recorded before Gate A), or leave the repo and retry later after the
     failure is understood. Report both SHAs in the failure summary.

Do not restart the bridge, deploy another node, send a canary, move
credentials, or publish a release unless that exact action is separately
authorized. Attended node restarts are the operator's job; the pre-approved
unattended path is `scripts/ccc-self-update.sh`, not this skill.
