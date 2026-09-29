- **Skills intake revise: three fail-closed gaps from the A2AD round closed
  (#1460 P1).** `skills-intake-revise-handler.sh` no longer takes the LAST
  outcome object in the reviser output (`reversed(candidates)`): a planted
  or echoed `drop_recommendation` after a real revision used to win silently.
  Byte-identical repeats of one object are deduped; two or more DISTINCT
  outcome objects are now a handler failure. A `revised` result whose file
  set (paths and contents, order-insensitive) is byte-identical to the
  packet's `skillFiles` is also a handler failure ("no-op revision") instead
  of a `pass` validation. `a2a-intent-dispatcher.sh` now fails loudly (exit
  1, reason on stderr) for any other `*skill*intake*` intent — singular,
  version-suffixed, schema-id, re-cased or misspelt forms — instead of
  handing it to the generic handler, which acked it. Fleet install
  verification for #1460 is unchanged and still open.
