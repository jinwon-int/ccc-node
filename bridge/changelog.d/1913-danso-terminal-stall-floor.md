- **Danso: the terminal-stall guard no longer cuts a model request that is
  still thinking (#1913).** A non-streaming Danso model request sends no
  events until it answers, so the shared `CCC_TERMINAL_STALL_SECONDS` guard
  (300s) read long GLM reasoning calls as a vanished completion. On gongmyoung
  the guard equalled the 300s provider timeout, so the bridge killed the
  request before Danso could answer or retry (`reason=signal_termination`
  exactly 300s after the request). Auto-resume then repeated the same cut,
  13 times in one day, with 30% turn success against 85–95% on nodes with the
  same model and key. For danso turns the configured grace is now a minimum,
  raised to `CCC_DANSO_PROVIDER_TIMEOUT_SECONDS` × 4 bounded wire attempts
  + 90s (1290s at 300s, 810s at the 180s default). `0` still disables the
  guard, and other providers are unchanged.
