- **The Matrix frontend now says when its crash loop is over (#2086).**
  `matrix_crash_loop` alerted when a streak of rapid unclean exits began and
  repeated while it lasted, but the end of the streak reset the record
  silently, so the owner never learned the frontend had recovered. A
  timer started after the start-up back-off now marks the run stable after
  `CCC_PROCESS_CRASH_WINDOW_SECONDS`; if the streak had alerted, one
  `matrix_crash_loop_recovered` notice is spooled with the streak length,
  the outage duration and the last exit's exception class name — a
  constant template, never an exception message. A start that finds the
  previous run outlived the window while the streak was still unannounced
  sends it instead (exactly once either way). A streak that never alerted
  ends silently. An orderly stop is not a recovery: the notice waits for a
  later run that stays up. The notice has its own code and so its own
  spool dedup key, so it is never folded into a crash-loop alert and never
  suppresses a later streak's. `crash-budget.json` gains an optional
  `unrecovered` snapshot; records written by the previous version are read
  as before, and an alerted streak recorded by it is still owed its notice.
