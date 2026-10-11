- **Self-update notices go to each node's own Matrix room by default (#2242).**
  `CCC_PUSH_FLEET_RELAY_DIRECT_EVENTS` now defaults to `SelfUpdate` (was
  empty), so in fleet relay mode a node's ccc-node self-update notice takes
  the #2227 owner-room path instead of the 🔔 fleet alerts room; every other
  record still goes to the relay. An explicit value replaces the default
  (list `SelfUpdate` to keep it; an empty value sends everything to the
  relay). Nodes with `~/.claude/self-update.notify` = `none` stay silent.
