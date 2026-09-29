- **Docs: occupancy check before a manual bridge restart (#1881).**
  `docs/bridge-ops.md` now names `health.json` →
  `workload.turn_occupancy.state` / `active_requests` /
  `oldest_request_age_seconds` as the canonical pre-restart check, with a
  copy-paste `jq` one-liner, a fail-closed `jq -e` gate, freshness guidance,
  and a warning that `active_turns` / `workload.active` do not exist (an
  automated idle check reading them restarted two busy nodes on 2026-09-21).
  CONTRIBUTING and the Danso guide link to it. No code or behaviour change.
