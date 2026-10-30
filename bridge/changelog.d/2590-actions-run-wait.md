- **External waits can now watch a GitHub Actions workflow run (#2590).**
  `external_wait_cli register --source actions-run --repo O/R --run-id N`
  registers a durable wait on one run (a dispatched build, a bundle, a release
  upload) instead of a PR. The run is resolved through `gh` at registration
  (an unknown run fails closed); its workflow name, run number and head are
  recorded for display. The monitor polls `repos/{repo}/actions/runs/{id}`,
  backing off to 2 minutes, and ends the wait `success` (conclusion
  `success`/`neutral`), `cancelled`, or `failure` (any other conclusion,
  passed to the resumed turn as `conclusion=<value>`). A run held by an
  environment approval (`status: waiting`) stays open; the wait-status line
  shows `(awaiting approval)` and is refreshed when the live status changes.
  `/waits`, wake notifications and the continuation prompt name the run
  (`repo Actions run <id>`) instead of a PR. `gh` "not found" errors are now
  classified as `not-found` (still bounded: three failed reads end the wait as
  `monitor-error`).
