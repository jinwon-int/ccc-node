- **Test-only: remove two bridge-tests timing races (#2731).** The hanging
  loader in `test_interrupt_reaps_real_materializer_and_loader` now publishes
  its pidfile through a temp file and `os.replace`, so the poll on `exists()`
  can no longer read an empty pidfile (`ValueError: ... ''`, 3.14).
  `test_delegated_lifecycle_defers_terminal_guard_until_distinct_limit` now
  keeps the 5 ms terminal guard off until the delegated `started` event has
  reached the turn state, instead of relying on that event being applied
  within 5 ms (3.11 flake); after that the configured bound applies unchanged,
  so a guard left armed during delegated work still fails the test. No bridge
  runtime code changes.
