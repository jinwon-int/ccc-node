- **Matrix `/revert` (#2004).** Matrix has no inline keyboard, so Telegram's
  two-step revert is now a numbered text menu: `/revert` lists the last 50 user
  messages (newest first, ten per page via `/revert page <p>`), a bare number
  picks one, a second number picks one of the same five modes;
  `/revert <n> [<mode>]` and `/revert cancel` skip or close the menu. Owner-only
  (#1955), same provider gates and answers as Telegram. HandeBot inherits it.
- A pending menu is per sender per room, in memory, and expires after 5
  minutes. Only a bare number answers it; an ordinary message or another
  command closes it and runs normally, and a number after expiry gets a "menu
  expired" answer instead of reaching the agent.
- The transcript rewrite (path guard plus atomic truncation) moved to the
  shared `revert.revert_conversation`, which Telegram's
  `_execute_conversation_revert` now calls too; the mode labels come from the
  shared `revert.REVERT_MODES`. Telegram behaviour is unchanged.
