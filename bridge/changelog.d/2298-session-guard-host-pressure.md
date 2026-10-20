- **Session guard evicts on RSS only under real host memory pressure (#2298).**
  On a 4 GiB host the RSS watermark sits at its 1024 MiB floor and an idle
  bridge tree (bridge, `claude`, a few stdio MCP servers) already reaches it,
  so `memory_high` fired on almost every guard tick while more than half of
  the host's memory was still available. Each eviction only bought a cold
  start of the CLI and its MCP servers. `CCC_BRIDGE_SESSION_TREE_RSS_LIMIT_MB`
  is now a necessary condition: idle sessions are evicted for RSS (and an
  idle Codex runtime is recycled with `rss-high`) only while `/proc/meminfo`
  `MemAvailable` is also below `max(512 MiB, 15% of MemTotal)`. If
  `MemAvailable` cannot be read (restricted `/proc`, older kernel), the
  watermark alone decides as before, so pressure protection never silently
  disappears. TTL and LRU-cap eviction are unchanged, and busy sessions stay
  spared (#2294). The eviction log line now names the branch and the host
  state, e.g. `reason=rss:1 ... mem_available_mb=256.0` (`ttl` / `lru` /
  `rss`; `n/a` when not probed or unreadable).
