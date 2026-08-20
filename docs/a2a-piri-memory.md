# A2A Piri lane memory snapshot producer

Host-side producer for the a2a-docker-runner piri memory extension
(jinwon-int/a2a-nexus#1797 item 3a, runner wiring in #1890/#1892).

`scripts/a2a_piri_memory_snapshot.py` materializes the **shared-audience**
memory snapshot through the bridge's `memory_audience` stack — the exact
route group/channel conversations use — and atomically publishes it, owner
`0600`, to `/var/lib/a2a-runner/piri-memory/MEMORY.md`. Private DM stores and
private-only legacy inputs are never read on this route, so the published
file is safe to enter A2A task containers. Any failure leaves the previous
snapshot untouched (fail-closed, exit 78; usage errors exit 2).

The runner mounts the directory read-only at `/run/secrets/piri-memory` when
the lane opts in (`A2A_DOCKER_RUNNER_PIRI_MEMORY_ENABLED=1`); the baked
extension injects the snapshot into the piri system prompt bounded by
`A2A_PIRI_MEMORY_MAX_BYTES` (default 32768).

## Install (worker hosts)

```bash
install -m 0644 scripts/a2a-piri-memory-snapshot.service /etc/systemd/system/
install -m 0644 scripts/a2a-piri-memory-snapshot.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now a2a-piri-memory-snapshot.timer
# one-shot manual refresh / verification:
systemctl start a2a-piri-memory-snapshot.service
```

The unit assumes the canonical node layout (`/opt/ccc-node` checkout with the
bridge venv). Overrides via `Environment=`/`ExecStart=` drop-ins:
`--audience-root`, `--output`, `--max-bytes` (hard ceiling 131072).

## Tests

`python3 scripts/a2a_piri_memory_snapshot_test.py` (also wired into
`scripts/validate-harness.sh`).
