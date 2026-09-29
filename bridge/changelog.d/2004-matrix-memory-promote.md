- **Matrix `/memory_promote` (#2004).** `create_app` never passed the
  `memory_promoter` to `MatrixBot` and the command was not parsed, so on a
  Matrix node `/memory_promote distill-…` reached the agent as plain text. The
  promoter is now wired and the command mirrors Telegram's contract and
  answers: owner-only (#1955), available only in `audience-scoped` memory mode
  with the promoter and local sink present (otherwise a plain "unavailable"
  answer), accepted only in the owner's direct room (the Matrix-routed private
  scope), exactly one `distill-<12 hex>` id, then a shared-index refresh. Logs
  stay body-free.
