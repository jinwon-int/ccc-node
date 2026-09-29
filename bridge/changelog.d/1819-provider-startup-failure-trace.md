- **Provider startup failures now leave a trace (#1819).** The Piri RPC client
  keeps a bounded in-memory stderr tail (4 KiB, last 8 lines, 300 chars each,
  credential-redacted) instead of discarding stderr entirely, so a failed
  spawn logs the exit code and the wrapper's last words (e.g. `ccc-piri: real
  CLI unavailable`, exit 127). The user-facing error gains the exit code
  (`Piri runtime failed to start (exit 127)`), and the redacted cause is
  recorded in `health.json` `agent.last_error` until the next successful
  session start. The raw transport exception is still not chained, since it
  may carry credentials; the redacted cause is logged instead.
- The turn `runtime-exception` branch, the Piri memory-bootstrap failure and
  the Matrix transport's turn-error path now log the error type, a redacted
  bounded message and the raise site; before, they left no log line at all.
