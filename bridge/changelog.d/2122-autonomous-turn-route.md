- **Autonomous between-turns continuations get the conversation route (#2122).**
  When the Claude CLI continued on its own after a background-task
  notification, the bridge's user turn had already ended and cleared its
  active-turn route, so external-wait and continuation registrations in that
  window failed `route-unavailable` and every approval was denied
  `turn=none` — read as a route vanishing mid-turn. The session now announces
  the autonomous window through an optional `set_unsolicited_lifecycle` seam;
  the bridge publishes the route for it under the `autonomous` owner and
  clears it after the terminal result. Route entries carry an `owner` stamp
  (`turn:<generation>` or `autonomous`) and `clear_active_turn` removes only
  its own, so one turn's `finally` can no longer erase a route another turn
  of the same conversation re-published. Approval denials in that window keep
  the same fail-closed decision but log `turn=autonomous` with their own
  message. Both CLIs' `route-unavailable` payloads now include
  `fresh_routes`, `reason` (`none`/`ambiguous`) and `routes_path`.
