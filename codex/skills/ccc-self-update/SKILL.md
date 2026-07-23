---
name: ccc-self-update
description: Check ccc-node source and installed harness drift, preview a transactional update, and apply it with backup and rollback verification. Use when asked to update, upgrade, synchronize, or compare a ccc-node harness with GitHub main; applying always requires explicit operator approval.
---

# CCC Self Update

1. Locate the serving checkout and distinguish source sync from service
   restart:

   ```bash
   ROOT="${CCC_NODE_ROOT:-/opt/ccc-node}"
   bash "$ROOT/scripts/ccc-bridge-locate.sh" --json
   git -C "$ROOT" fetch origin main
   git -C "$ROOT" status --short --branch
   git -C "$ROOT" log --oneline HEAD..origin/main
   ```

2. Report the pending commits and affected harness assets. Stop if the
   checkout is dirty or diverged.

3. After explicit approval, update through a branch-safe, ff-only path:

   ```bash
   git -C "$ROOT" pull --ff-only
   "$ROOT/setup.sh" --dry-run
   ```

4. Show the dry-run plan, including Codex managed skills. Apply `setup.sh` only
   with explicit approval. The installer must preserve user-authored skills,
   reject unsafe collisions, and roll back a partial managed-skill transaction.

5. Validate:

   ```bash
   bash "$ROOT/scripts/validate-harness.sh"
   git -C "$ROOT" status --short --branch
   ```

Do not restart the bridge, deploy another node, send a canary, move
credentials, or publish a release unless that exact action is separately
authorized.
