- **fix(skills): the listing policy sizes the budget from the bridge model when `settings.json` has none (#2358).**
  Nodes that pin the bridge model with `ANTHROPIC_MODEL` in a systemd drop-in
  and leave `settings.json` without `model` were budgeted at the 200k default
  (16,000 chars) although the bridge runs a 1M model (≈80,000 chars), so
  recently used skills were name-only'd that would have fit. When the
  `settings.json` model is missing or unrecognised, `ccc-skill-listing-policy`
  now uses `ANTHROPIC_MODEL` from its own environment, then from the bridge
  unit (`ccc-matrix-bridge.service`, then `ccc-telegram-bridge.service`;
  `systemctl show` read-only, `Environment=` then `EnvironmentFiles=` in
  systemd's override order, only that key is read), before the 200k default.
  `plan`/`apply` name the source (`bridge <scope> <unit> ANTHROPIC_MODEL …`).
  A node with a `settings.json` model, or without systemd, behaves as before;
  `CCC_SKILL_LISTING_SYSTEMCTL=off` disables the lookup.
