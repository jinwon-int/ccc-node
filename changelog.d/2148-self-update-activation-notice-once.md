- **self-update pages once per unresolved target generation (#2148).** On a
  Termux node running a prepared generation, every new `main` commit made the
  changed tick fail activation and each unchanged tick after it re-report the
  same serving mismatch — two owner alarms a day per node for one cause. The
  activation-incomplete notices (restart failure, external restart failure or
  serving mismatch on the changed tick; serving mismatch or unknown identity
  on unchanged ticks) now go out once per target, remembered in
  `self-update.activation-notified`; later ticks log
  `notify=suppressed reason=already-notified`, still audit and still exit 14.
  A verified activation/reconciliation clears the marker and a new target
  replaces it. Unhealthy-runtime and corrupt-record notices are unchanged.
