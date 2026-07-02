# Distill fleet closeout guide

> Historical closeout guide. Current distill/memory docs live in [`../memory.md`](../memory.md); current docs index is [`../README.md`](../README.md).

Issue #82 tracks fleet verification of the Session Distiller. Closeout is intentionally conservative because a green result on one node does not prove the rest of the fleet has the same state directory, Honcho reachability, queue/drain behavior, or Claude subprocess behavior.

## Closeout rule

Keep the meta tracker open until one of these is true:

1. every node checklist item has live verification evidence, or
2. every remaining unticked node has a dedicated blocker subissue owned by the finalizer.

Do not close the meta tracker only because source/test fixes merged. Source fixes can make verification possible, but they are not evidence that each node has self-updated and completed `/distill status` plus a manual `/distill` run.

## Evidence contract for a verified node

A node can be treated as verified when the finalizer has non-secret evidence that:

- `/distill status` or `scripts/ccc-distill-check.sh --json` reports sensible local state;
- the distiller is in the intended mode (`LIVE` unless explicitly scoped otherwise);
- queue and dead-letter counts are understood;
- a manual distill trigger completed and produced a last-result summary;
- Honcho push reachability is demonstrated without printing tokens or raw session content;
- node-specific caveats are resolved, such as non-root `CCC_STATE_DIR`, gwakga loopback Honcho behavior, yukson memory pressure, or daegyo Termux/service-management differences.

## Blocker subissue rule

When verification is still missing, prefer one blocker subissue per unticked node over closing the gap in prose comments. A blocker subissue should include:

- node name;
- missing evidence;
- known caveats;
- safe next check;
- explicit approval boundary: no service restart, DB mutation/prune/replay, reboot, destructive cross-node mutation, secret output, or cross-node write/install/service changes unless separately approved.

Avoid closing keywords in worker-generated PR bodies or comments for the meta tracker. The finalizer owns the decision to close issue #82 after either full verification or blocker-subissue coverage is present.
