- **setup: every node-local top-level `settings.json` key now survives a
  re-render (#1920).** Only `model` (#1235), node-local `env` keys (#1402) and
  the three skill-listing keys (#2011 A) were carried across, so any other key
  a node or the CLI wrote — `effortLevel` via `/effort`, `alwaysThinkingEnabled`,
  `modelSettings`, … — vanished on the next self-update tick. setup.sh now
  applies the `env` rule at the top level: a key that
  `claude/settings.base.json` and `claude/hooks/enforcement-overlay.json` do
  not declare is node-local and is re-applied; template-declared keys stay
  repo-owned and win. `model` keeps its own handler. A key removed from the
  templates in future must be listed in `RETIRED_SETTINGS_KEYS` so nodes do
  not keep the stale repo value as "node-local".
