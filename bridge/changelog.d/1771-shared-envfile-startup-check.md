- **Shared bridge EnvironmentFile + startup required-env check (#1771,
  follow-up to #2065).** Both bridge systemd units now read one owner-only
  `EnvironmentFile=-~/.config/ccc-node/bridge.env` (optional `-` path): the
  Telegram unit renderer (`reconcile` adds the line to existing generated
  units with a daemon-reload only; a foreign `EnvironmentFile=` stays bespoke)
  and `service-systemd-matrix.service.example`, replacing hand-mirrored
  provider drop-ins. Secrets such as `CLAUDE_CODE_OAUTH_TOKEN` reach the
  provider only through this process environment, never via app-level
  injection. Migration steps: `docs/bridge-ops.md` → "Provider environment
  contract".
- At startup the bridge checks what the selected provider needs against the
  environment its child will get (process env + #2065 wrapper keys): the
  piri/codex CLI and the `ccc-*` wrapper's real CLI, or a Claude auth source.
  A gap is logged as one ERROR naming the keys (never values) and the bridge
  runs degraded: Telegram reports it as the readiness reason, and the Matrix
  frontend records it in `health.json` instead of marking the agent healthy
  unconditionally.
