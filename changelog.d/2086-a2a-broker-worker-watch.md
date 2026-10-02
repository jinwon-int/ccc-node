- **A2A broker worker staleness watch (#2086).** A node that loses DNS can no
  longer heartbeat its A2A worker, and the broker marks it `stale` in
  `GET /workers`, but nothing paged anyone. The new
  `scripts/a2a-broker-worker-watch.py` reads that view (edge secret sourced by
  bash and passed to curl on stdin only) as an agent-cron command task on the
  broker host. A node pages as `DOWN <node> source=broker:<name>
  reason=worker-stale age=...` only after it has been non-online for 15 minutes,
  then re-pages after 1h and every 6h. An unreachable or auth-rejected broker
  is one `UNREACHABLE`/`DEGRADED broker:<name>` finding; worker state is frozen
  meanwhile, and a broker restart or mass flip opens a grace window. Exclusion
  list, optional recovery pages, owner-only atomic state, and exit codes
  0/1/2 that fit `telegram-owner-on-failure`. See
  `docs/a2a-broker-worker-watch.md`. No task is installed by this change.
