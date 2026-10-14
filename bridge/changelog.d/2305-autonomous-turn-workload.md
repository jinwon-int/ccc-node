- **Restart gating no longer reports a provider autonomous turn as idle
  (#2305).** After a run-in-background task finishes, Claude Code injects its
  `<task-notification>` and continues without a bridge-submitted turn. The
  agent-session registry only counts `send_turn` turns and the finished task had
  already left the background tracker, so `workload_snapshot()` returned 0 and
  `health.json` published `turn_occupancy: idle` — on both the Telegram and
  Matrix reporters — while the CLI was still running tools. Self-update's idle
  gate, `termux-restart-frontends.sh` and the operator check in
  `docs/bridge-ops.md` all trust that value. The #2291 seam
  (`autonomous_turn_inflight`) only reached the session resource guard.
  `ProjectChatHandler.workload_snapshot()` now folds each resident session's
  autonomous turn in, through a new Claude `autonomous_workload_snapshot(now)`
  that ages the turn from its first frame (falling back to the boolean seam with
  age 0 for runtimes that only expose that). A dead reader still clears it.
  `foreground_workload_snapshot()` is unchanged: no bridge `wait_for` bounds an
  autonomous turn, so the request-lifetime alert must not see it.
