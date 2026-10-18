- **skill-promotion L3: a pretty-printed relay verdict is no longer read as a
  failure (#2378).** `approve-via-relay.sh` ends with a multi-line `jq -n`
  object, but `_run_relay_approval` parsed only the last stdout line (`}`), so
  the first live approval (fleet-skills#514, 2026-10-19) was recorded as
  `approve_output_invalid` although GitHub held the exact-head review, and the
  head-bound merge was skipped. The publisher now parses from the last line that
  opens an object to the end; compact output and leading log lines still work,
  and the ok/approved/exact-head checks are unchanged.
