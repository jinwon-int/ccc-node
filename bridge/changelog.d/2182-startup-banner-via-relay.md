- **Matrix startup banner follows fleet relay mode (#2182).** With
  `CCC_PUSH_FLEET_RELAY_URL` set, the `🟢 … 기동` banner is queued as a
  push-spool `Startup` record (hourly dedup key unchanged) and relayed to
  `@fleet-alerts`, instead of being posted into the owner's room as the
  agent on every restart. `push_notifier.write_spool_record` is the shared
  atomic record writer.
