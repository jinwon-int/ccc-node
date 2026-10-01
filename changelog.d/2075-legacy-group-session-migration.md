- **Operator tool to clear group session rows that still resume a DM session
  (#2075).** #2092 fixed how the runners pick a session, but rows already
  written before it — a group/room row seeded from the legacy `<uid>` row, or
  saved after an old runner resumed the DM session on the room's stream —
  keep resuming the DM-derived session inside the room. New
  `scripts/ccc_session_scope_audit.py` flags a room row whose `session_id` is
  also held by a DM/legacy row (`dm-session`) or by another room
  (`cross-room-session`); the `0:0` shared-all row is ignored. It is dry-run by
  default and prints counts and row keys only. `--apply` refuses while the
  owning bridge runs or a pending external wait/continuation in a flagged room
  is bound to a flagged id, copies the audited bytes to `sessions.json.bak-2075-<utc>`, then
  gives each flagged room row what `/new` persists (`session_id: null`,
  `new_session: true`) without deleting it, so the first-use seed cannot copy
  the DM id back. DM rows are never modified; a re-run is a no-op.
  `ccc-doctor` adds a read-only `session scope rows` warning with the count
  (exit code unchanged). Procedure: `docs/bridge-ops.md`.
