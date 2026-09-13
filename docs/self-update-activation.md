# Installed generation and pending activation (#1527)

The installed SHA describes successful setup. It does not establish that a
runtime started serving that generation. Before advancing that marker, the
updater durably writes `self-update.pending-activation.json` under its existing
state lock. Failed persistence exits 14 before restart and preserves the install
recovery snapshot. Failed activation retains pending evidence and the snapshot.

A healthy old runtime on the next unchanged tick remains incomplete (14). Pending
state does not itself schedule a restart or retry. The existing unhealthy-runtime
recovery policy and operator-configured restart commands remain in effect.
Successful allowlisted restarts, or an external/recovery restart and its configured
health check, complete activation under that existing operator contract.

Optional unchanged-tick reconciliation uses `self-update.serving-generation-cmd`
(or `CCC_SELF_UPDATE_SERVING_GENERATION_CMD`). This protected operator command
must emit the existing bridge health JSON, including `process.started_at`,
`process.pid`, and the frozen `ccc.runtime-generation.v1` object. It must identify
the exact full target SHA with clean tracked source, the expected `<repo>/bridge`
source directory, and no generation collection errors. The process must be alive,
start after this activation attempt began, and have a health update within 150
seconds with service available, Telegram healthy and agent healthy. The optional
separate health command must also pass. Probe output is bounded to 32 KiB and the
configured restart wait budget; inherited background writers cannot extend it.

`bridge/runtime_generation.py` captures this identity at process initialization;
`bridge/utils/health.py` preserves it across heartbeats. A command printing
`git rev-parse HEAD`, a legacy short SHA, missing startup identity, or a healthy
old process cannot reconcile pending state. Upgrade legacy health producers before
using this reconciliation path. This is source-generation evidence, not dependency
attestation, authentication completion, or a new activation/deployment authority.

State is owner-only mode 0600, bounded, and read without following symlinks.
Writes use the shared secure-fs atomic writer with file and directory fsync and a
transaction intent. Dangling links, hardlinks, unsafe modes, truncated JSON, intent
files, and both old/new temp-name residues fail closed and are left for operator
inspection. Completion durably replaces pending state with `outcome: activated`;
the bounded terminal receipt remains on disk so unlink failure cannot erase the
only evidence. Do not use file existence alone as a pending-state check.
An unsupported directory fsync is a persistence failure. Recovery snapshots are
never automatically restored by pending-state reconciliation.
