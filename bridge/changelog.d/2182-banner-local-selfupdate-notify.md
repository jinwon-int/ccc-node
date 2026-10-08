- **Startup banner stays in the agent's own room; self-update notify mode
  (#2182).** Owner decision 2026-10-09: the Matrix `🟢 … 기동` banner is
  per-agent, so it is posted in the agent's room again even in fleet relay
  mode (reverts the #2192 spool routing). `ccc-self-update.sh` gains
  `~/.claude/self-update.notify` / `CCC_SELF_UPDATE_NOTIFY` (`all` default,
  `none`): `none` queues no owner notification at all and only logs
  `notify=suppressed`, for nodes whose channel must stay quiet on updates.
