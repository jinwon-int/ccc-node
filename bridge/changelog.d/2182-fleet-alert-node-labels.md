- **Fleet alert sender prefix (#2182).** The relay receiver prefixes every
  queued alert with `[<agent name>] ` (from the optional `--labels` JSON on
  the relay host, re-read on change; unknown nodes show their id) so the
  owner sees which agent an alert came from in the first line.
